"""Workflow capture validation, binding discovery and execution-copy building.

Only the execution copy is changed.  The stored UI/API workflow files remain
byte-for-byte as captured, and every attempt is built from a deep copy of the
frozen API workflow.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from .errors import GachaError
from .util import canonical_json, sha256_text

KNOWN_SEED_NODES = {
    "KSampler": ("seed", 0, 0xFFFFFFFFFFFFFFFF),
    "KSamplerAdvanced": ("noise_seed", 0, 0xFFFFFFFFFFFFFFFF),
}
SEED_DOMAIN_MAX = 9007199254740991
SEED_INPUT_NAMES = {"seed", "noise_seed", "rand_seed"}
OUTPUT_NODE_TYPES = {"SaveImage": "SaveImage", "PreviewImage": "PreviewImage"}


@dataclass
class SeedBinding:
    node_id: str
    input_name: str
    minimum: int
    maximum: int
    node_class: str
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "input_name": self.input_name,
            "min": self.minimum,
            "max": self.maximum,
            "node_class": self.node_class,
            "active": self.active,
        }


@dataclass
class PreflightResult:
    ok: bool = True
    load_image_node_id: str = ""
    save_image_node_id: str = ""
    seed_bindings: list[SeedBinding] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    upstream_node_ids: list[str] = field(default_factory=list)
    extra_output_nodes: list[dict[str, Any]] = field(default_factory=list)
    workflow_hash: str = ""
    save_has_downstream: bool = False
    load_image_upstream: bool = False

    def add_error(self, code: str, message: str, **details: Any) -> None:
        self.ok = False
        self.errors.append({"code": code, "message": message, "details": details})

    def add_warning(self, code: str, message: str, **details: Any) -> None:
        self.warnings.append({"code": code, "message": message, "details": details})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "load_image_node_id": self.load_image_node_id,
            "save_image_node_id": self.save_image_node_id,
            "seed_bindings": [binding.to_dict() for binding in self.seed_bindings],
            "warnings": self.warnings,
            "errors": self.errors,
            "upstream_node_ids": self.upstream_node_ids,
            "extra_output_nodes": self.extra_output_nodes,
            "workflow_hash": self.workflow_hash,
            "save_has_downstream": self.save_has_downstream,
            "load_image_upstream": self.load_image_upstream,
        }

    def require_ok(self) -> None:
        if self.ok:
            return
        first = self.errors[0] if self.errors else {"code": "WORKFLOW_UNSUPPORTED", "message": "工作流预检失败。"}
        code = first["code"]
        if code not in {
            "WORKFLOW_UNSUPPORTED",
            "NODE_MISSING",
            "SEED_BINDING_INVALID",
            "PATH_NOT_ALLOWED",
        }:
            code = "WORKFLOW_UNSUPPORTED"
        raise GachaError(
            code,
            first["message"],
            details={"errors": self.errors, "warnings": self.warnings},
        )


def _validate_api_workflow(workflow: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(workflow, dict) or not workflow:
        raise GachaError("WORKFLOW_UNSUPPORTED", "API 工作流为空或格式错误。")
    nodes: dict[str, dict[str, Any]] = {}
    for raw_id, node in workflow.items():
        node_id = str(raw_id)
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str):
            raise GachaError(
                "WORKFLOW_UNSUPPORTED",
                "API 工作流节点缺少 class_type。",
                details={"node_id": node_id},
            )
        inputs = node.get("inputs")
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise GachaError(
                "WORKFLOW_UNSUPPORTED",
                "API 工作流节点 inputs 必须是对象。",
                details={"node_id": node_id},
            )
        nodes[node_id] = {"class_type": node["class_type"], "inputs": inputs, "_meta": node.get("_meta")}
    return nodes


def _linked_node_id(value: Any) -> Optional[str]:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return str(value[0])
    return None


def _upstream_ids(nodes: Mapping[str, Mapping[str, Any]], start_id: str) -> tuple[set[str], list[dict[str, Any]]]:
    seen: set[str] = {start_id}
    missing: list[dict[str, Any]] = []
    stack = [start_id]
    while stack:
        current = stack.pop()
        node = nodes.get(current)
        if node is None:
            continue
        for input_name, value in node["inputs"].items():
            linked = _linked_node_id(value)
            if linked is None:
                continue
            if linked not in nodes:
                missing.append({"node_id": current, "input_name": input_name, "linked": linked})
                continue
            if linked not in seen:
                seen.add(linked)
                stack.append(linked)
    return seen, missing


def _downstream_exists(nodes: Mapping[str, Mapping[str, Any]], node_id: str) -> bool:
    for other_id, node in nodes.items():
        if other_id == node_id:
            continue
        for value in node["inputs"].values():
            if _linked_node_id(value) == node_id:
                return True
    return False


def _check_ui_workflow(ui_workflow: Any, selected_ids: Iterable[str], result: PreflightResult) -> None:
    if not isinstance(ui_workflow, dict) or not isinstance(ui_workflow.get("nodes"), list):
        result.add_warning(
            "UI_WORKFLOW_MISSING",
            "未收到可复现的编辑器工作流快照；任务仍可使用 API 工作流执行，但无法从任务恢复编辑器视图。",
        )
        return
    ui_ids = {str(node.get("id")) for node in ui_workflow.get("nodes", []) if isinstance(node, dict)}
    for selected in selected_ids:
        if selected not in ui_ids:
            result.add_error(
                "WORKFLOW_UNSUPPORTED",
                "所选节点无法映射到编辑器工作流（可能位于子图）。V1.0 不支持子图绑定。",
                node_id=selected,
            )


def preflight(
    workflow_ui: Any,
    workflow_api: Any,
    bindings: Mapping[str, Any],
    *,
    node_info_provider: Optional[Any] = None,
) -> PreflightResult:
    """Validate the frozen workflow and its input/output/seed bindings.

    ``node_info_provider`` may be a callable taking a class_type and returning
    the installed ComfyUI ``object_info`` entry (or None).  When omitted only
    structural checks run; the live adapter still validates at submission.
    """
    result = PreflightResult()
    load_id = str(bindings.get("load_image_node_id") or "").strip()
    save_id = str(bindings.get("save_image_node_id") or "").strip()
    result.load_image_node_id = load_id
    result.save_image_node_id = save_id
    if not load_id or not save_id:
        result.add_error("WORKFLOW_UNSUPPORTED", "必须明确选择一个 LoadImage 和一个最终 SaveImage。")
    try:
        nodes = _validate_api_workflow(workflow_api)
    except GachaError as exc:
        result.add_error(exc.code, exc.message, **exc.details)
        result.workflow_hash = sha256_text(canonical_json(workflow_api))
        return result
    result.workflow_hash = sha256_text(canonical_json(workflow_api))

    if load_id and load_id not in nodes:
        result.add_error(
            "NODE_MISSING",
            "绑定的 LoadImage 不在 API 工作流中。",
            node_id=load_id,
            available=sorted(nodes),
        )
    if save_id and save_id not in nodes:
        result.add_error(
            "NODE_MISSING",
            "绑定的 SaveImage 不在 API 工作流中。",
            node_id=save_id,
            available=sorted(nodes),
        )

    if result.errors:
        return result

    load_node = nodes[load_id]
    save_node = nodes[save_id]
    if load_node["class_type"] != "LoadImage":
        result.add_error(
            "WORKFLOW_UNSUPPORTED",
            "输入节点必须是标准 LoadImage。",
            node_id=load_id,
            class_type=load_node["class_type"],
        )
    if save_node["class_type"] != "SaveImage":
        result.add_error(
            "WORKFLOW_UNSUPPORTED",
            "输出节点必须是标准 SaveImage（V1.0 只接管一个最终 SaveImage）。",
            node_id=save_id,
            class_type=save_node["class_type"],
        )
    if not isinstance(load_node["inputs"].get("image"), str):
        result.add_error(
            "WORKFLOW_UNSUPPORTED",
            "LoadImage 的 image 输入必须是可替换的文件名，不能是连线。",
            node_id=load_id,
        )
    if save_node["inputs"].get("images") is None:
        result.add_error(
            "WORKFLOW_UNSUPPORTED",
            "SaveImage 缺少 images 输入。",
            node_id=save_id,
        )
    if not result.errors:
        _check_ui_workflow(workflow_ui, [load_id, save_id], result)
        if result.errors:
            return result

    upstream, missing_links = _upstream_ids(nodes, save_id)
    result.upstream_node_ids = sorted(upstream)
    for missing in missing_links:
        result.add_error(
            "NODE_MISSING",
            "工作流连线指向不存在的节点。",
            **missing,
        )
    result.load_image_upstream = load_id in upstream
    if not result.load_image_upstream:
        result.add_error(
            "WORKFLOW_UNSUPPORTED",
            "所选 LoadImage 不在最终 SaveImage 的上游依赖路径中。",
            load_image_node_id=load_id,
            save_image_node_id=save_id,
        )
    result.save_has_downstream = _downstream_exists(nodes, save_id)

    # Extra outputs are left untouched, but the user must know they save files
    # outside Gacha Master's control.
    for node_id, node in nodes.items():
        class_type = node["class_type"]
        if class_type in OUTPUT_NODE_TYPES and node_id != save_id:
            result.extra_output_nodes.append({"node_id": node_id, "class_type": class_type})
            if class_type == "SaveImage":
                result.add_warning(
                    "EXTRA_SAVE_IMAGE",
                    "工作流还包含另一个 SaveImage；它会按原行为额外保存文件，不纳入抽卡大师管理。",
                    node_id=node_id,
                )
    if result.extra_output_nodes:
        result.add_warning(
            "EXTRA_OUTPUT_NODES",
            "工作流包含额外输出节点，会产生插件无法管理的文件。",
            nodes=result.extra_output_nodes,
        )

    # Seed bindings: only nodes on the path to the selected output matter.
    for node_id in sorted(upstream):
        node = nodes[node_id]
        class_type = node["class_type"]
        seed_spec = KNOWN_SEED_NODES.get(class_type)
        inputs = node["inputs"]
        if seed_spec is not None:
            input_name, minimum, maximum = seed_spec
            if input_name not in inputs:
                result.add_error(
                    "SEED_BINDING_INVALID",
                    f"{class_type} 缺少 {input_name} 输入。",
                    node_id=node_id,
                )
                continue
            raw = inputs[input_name]
            if _linked_node_id(raw) is not None:
                result.add_error(
                    "SEED_BINDING_INVALID",
                    f"{class_type} 的 {input_name} 连接了其他节点，V1.0 不解除种子连线。请改为直接数值输入。",
                    node_id=node_id,
                    input_name=input_name,
                )
                continue
            if isinstance(raw, bool) or not isinstance(raw, int):
                result.add_error(
                    "SEED_BINDING_INVALID",
                    f"{class_type} 的 {input_name} 必须是整数。",
                    node_id=node_id,
                    input_name=input_name,
                    value_type=type(raw).__name__,
                )
                continue
            active = True
            if class_type == "KSamplerAdvanced" and inputs.get("add_noise") == "disable":
                active = False
                result.add_warning(
                    "INACTIVE_SEED",
                    "KSamplerAdvanced 的 add_noise 已禁用，该种子不会实际参与生成。",
                    node_id=node_id,
                )
            result.seed_bindings.append(
                SeedBinding(
                    node_id=node_id,
                    input_name=input_name,
                    minimum=max(0, int(minimum)),
                    maximum=min(int(maximum), SEED_DOMAIN_MAX),
                    node_class=class_type,
                    active=active,
                )
            )
            continue

        # An upstream custom node exposing a random-looking input would be an
        # uncontrolled random source.  Refuse instead of guessing its contract.
        for input_name, value in inputs.items():
            if str(input_name).lower() in SEED_INPUT_NAMES:
                result.add_error(
                    "SEED_BINDING_INVALID",
                    "上游存在 V1.0 未支持的自定义随机输入，无法保证受控种子。",
                    node_id=node_id,
                    unit_class=class_type,
                    input_name=input_name,
                )

    active_bindings = [binding for binding in result.seed_bindings if binding.active]
    if not active_bindings:
        result.add_error(
            "SEED_BINDING_INVALID",
            "生产抽卡至少需要一个可绑定的有效随机种子输入（KSampler.seed 或 KSamplerAdvanced.noise_seed）。",
        )
    if len(result.seed_bindings) > 1:
        result.add_warning(
            "MULTIPLE_SAMPLERS",
            "工作流包含多个受控采样器；V1.0 会为每个种子输入分配任务内唯一值并持久化完整映射。",
            count=len(result.seed_bindings),
        )

    # Structural validation of installed node classes, when the adapter can
    # answer.  Resource/model availability is checked by ComfyUI validation at
    # submission; missing classes are listed here with remedies.
    if node_info_provider is not None:
        missing_classes: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            class_type = node["class_type"]
            if class_type == "GachaSaveImage":
                continue
            try:
                info = node_info_provider(class_type)
            except Exception:
                info = None
            if info is None:
                missing_classes.append({"node_id": node_id, "class_type": class_type})
        if missing_classes:
            result.add_error(
                "NODE_MISSING",
                "已安装的 ComfyUI 未注册工作流所需节点。",
                nodes=missing_classes,
            )

    return result


def build_execution_prompt(
    workflow_api: Mapping[str, Any],
    *,
    load_image_node_id: str,
    save_image_node_id: str,
    staged_name: str,
    seeds: Mapping[str, int],
) -> tuple[dict[str, Any], str]:
    """Build a deep-copied execution prompt.

    ``seeds`` maps ``"node_id::input_name"`` to an integer.  The selected
    SaveImage is replaced in place by GachaSaveImage while keeping the same
    node ID and upstream ``images`` link, so downstream connections and the
    original output slot remain intact.
    """
    prompt = copy.deepcopy(dict(workflow_api))
    load_node = prompt.get(load_image_node_id)
    save_node = prompt.get(save_image_node_id)
    if not isinstance(load_node, dict) or not isinstance(save_node, dict):
        raise GachaError("WORKFLOW_UNSUPPORTED", "执行副本中缺少绑定节点。")
    load_node.setdefault("inputs", {})["image"] = staged_name
    images = save_node.get("inputs", {}).get("images")
    if images is None:
        raise GachaError("WORKFLOW_UNSUPPORTED", "SaveImage 缺少 images 输入。")
    token_inputs: dict[str, Any] = {"images": images, "gacha_token": "REPLACED_AT_SUBMIT"}
    # The token is injected by the scheduler with the real attempt UUID.
    _ = token_inputs
    for key, value in seeds.items():
        node_id, _, input_name = key.partition("::")
        node = prompt.get(node_id)
        if not isinstance(node, dict):
            raise GachaError("SEED_BINDING_INVALID", "执行副本中缺少种子节点。", node_id=node_id)
        node.setdefault("inputs", {})[input_name] = int(value)
    return prompt, sha256_text(canonical_json(prompt))


def apply_output_token(prompt: Mapping[str, Any], save_image_node_id: str, attempt_id: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(prompt))
    save_node = result.get(save_image_node_id)
    if not isinstance(save_node, dict):
        raise GachaError("WORKFLOW_UNSUPPORTED", "执行副本中缺少输出节点。")
    images = save_node.get("inputs", {}).get("images")
    meta = save_node.get("_meta")
    save_node["class_type"] = "GachaSaveImage"
    save_node["inputs"] = {"images": images, "gacha_token": attempt_id}
    if meta is not None:
        save_node["_meta"] = meta
    return result


def execution_workflow_hash(prompt: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(prompt))


def seed_keys(bindings: Iterable[SeedBinding]) -> list[str]:
    return [f"{binding.node_id}::{binding.input_name}" for binding in bindings]


def make_bindings_record(preflight_result: PreflightResult) -> dict[str, Any]:
    return {
        "load_image_node_id": preflight_result.load_image_node_id,
        "save_image_node_id": preflight_result.save_image_node_id,
        "seed_bindings": [binding.to_dict() for binding in preflight_result.seed_bindings],
        "workflow_hash": preflight_result.workflow_hash,
        "warnings": preflight_result.warnings,
    }
