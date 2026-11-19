/**
 * Starts the browser's shared session-stream worker (`./session-stream.worker.ts`). Apart from
 * `./session-stream` and loaded only in the browser, because `import.meta` (which the bundler
 * needs to find the worker) doesn't load where the modules are CommonJS (Playwright's specs).
 */
export function startWorker(): SharedWorker {
  return new SharedWorker(new URL("./session-stream.worker.ts", import.meta.url), { name: "session-stream", type: "module" });
}
