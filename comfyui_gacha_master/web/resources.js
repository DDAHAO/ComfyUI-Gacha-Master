// Central resource budgets and a read-only runtime snapshot used by the long
// navigation acceptance run.  Counts are measured from live structures, never
// reset before reporting.

import { globalCachedResultCount, livePagerCount, RESULT_PAGE_SIZE, MAX_CACHED_PAGES } from "./collections/result_pager.js";
import { thumbnailLoader } from "./components/lazy_image.js";

export const RESOURCE_BUDGETS = {
  sourceRows: 40,
  outputCards: 300,
  cachedOutputDtos: 1000,
  resultPagesPerSource: MAX_CACHED_PAGES,
  retainedResultSources: 5,
  thumbnailConcurrent: 8,
  compareImages: 6,
  resultPageSize: RESULT_PAGE_SIZE,
};

const providers = new Set();

export function registerResourceProvider(fn) {
  providers.add(fn);
  return () => providers.delete(fn);
}

export function resourceSnapshot() {
  const snapshot = {
    at: new Date().toISOString(),
    view: null,
    mountedSourceRows: 0,
    mountedOutputCards: 0,
    cachedOutputDtos: globalCachedResultCount(),
    liveResultPagers: livePagerCount(),
    images: thumbnailLoader.snapshot(),
    extra: {},
  };
  for (const provider of providers) {
    try {
      const value = provider();
      if (!value) continue;
      snapshot.mountedSourceRows += Number(value.mountedSourceRows || 0);
      snapshot.mountedOutputCards += Number(value.mountedOutputCards || 0);
      if (value.view) snapshot.view = value.view;
      Object.assign(snapshot.extra, value.extra || {});
    } catch (error) {
      snapshot.extra.error = String(error);
    }
  }
  snapshot.budgets = { ...RESOURCE_BUDGETS };
  snapshot.withinBudget = {
    sourceRows: snapshot.mountedSourceRows <= RESOURCE_BUDGETS.sourceRows,
    outputCards: snapshot.mountedOutputCards <= RESOURCE_BUDGETS.outputCards,
    cachedOutputDtos: snapshot.cachedOutputDtos <= RESOURCE_BUDGETS.cachedOutputDtos,
    thumbnailConcurrent: snapshot.images.active <= RESOURCE_BUDGETS.thumbnailConcurrent,
  };
  return snapshot;
}

export function installDebugProbe(win = globalThis) {
  if (!win) return;
  win.__gachaMasterResources = resourceSnapshot;
}
