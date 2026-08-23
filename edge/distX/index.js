var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// node_modules/unenv/dist/runtime/_internal/utils.mjs
// @__NO_SIDE_EFFECTS__
function createNotImplementedError(name) {
  return new Error(`[unenv] ${name} is not implemented yet!`);
}
__name(createNotImplementedError, "createNotImplementedError");
// @__NO_SIDE_EFFECTS__
function notImplemented(name) {
  const fn = /* @__PURE__ */ __name(() => {
    throw /* @__PURE__ */ createNotImplementedError(name);
  }, "fn");
  return Object.assign(fn, { __unenv__: true });
}
__name(notImplemented, "notImplemented");

// node_modules/unenv/dist/runtime/node/internal/perf_hooks/performance.mjs
var _timeOrigin = globalThis.performance?.timeOrigin ?? Date.now();
var _performanceNow = globalThis.performance?.now ? globalThis.performance.now.bind(globalThis.performance) : () => Date.now() - _timeOrigin;
var nodeTiming = {
  name: "node",
  entryType: "node",
  startTime: 0,
  duration: 0,
  nodeStart: 0,
  v8Start: 0,
  bootstrapComplete: 0,
  environment: 0,
  loopStart: 0,
  loopExit: 0,
  idleTime: 0,
  uvMetricsInfo: {
    loopCount: 0,
    events: 0,
    eventsWaiting: 0
  },
  detail: void 0,
  toJSON() {
    return this;
  }
};
var PerformanceEntry = class {
  static {
    __name(this, "PerformanceEntry");
  }
  __unenv__ = true;
  detail;
  entryType = "event";
  name;
  startTime;
  constructor(name, options) {
    this.name = name;
    this.startTime = options?.startTime || _performanceNow();
    this.detail = options?.detail;
  }
  get duration() {
    return _performanceNow() - this.startTime;
  }
  toJSON() {
    return {
      name: this.name,
      entryType: this.entryType,
      startTime: this.startTime,
      duration: this.duration,
      detail: this.detail
    };
  }
};
var PerformanceMark = class PerformanceMark2 extends PerformanceEntry {
  static {
    __name(this, "PerformanceMark");
  }
  entryType = "mark";
  constructor() {
    super(...arguments);
  }
  get duration() {
    return 0;
  }
};
var PerformanceMeasure = class extends PerformanceEntry {
  static {
    __name(this, "PerformanceMeasure");
  }
  entryType = "measure";
};
var PerformanceResourceTiming = class extends PerformanceEntry {
  static {
    __name(this, "PerformanceResourceTiming");
  }
  entryType = "resource";
  serverTiming = [];
  connectEnd = 0;
  connectStart = 0;
  decodedBodySize = 0;
  domainLookupEnd = 0;
  domainLookupStart = 0;
  encodedBodySize = 0;
  fetchStart = 0;
  initiatorType = "";
  name = "";
  nextHopProtocol = "";
  redirectEnd = 0;
  redirectStart = 0;
  requestStart = 0;
  responseEnd = 0;
  responseStart = 0;
  secureConnectionStart = 0;
  startTime = 0;
  transferSize = 0;
  workerStart = 0;
  responseStatus = 0;
};
var PerformanceObserverEntryList = class {
  static {
    __name(this, "PerformanceObserverEntryList");
  }
  __unenv__ = true;
  getEntries() {
    return [];
  }
  getEntriesByName(_name, _type) {
    return [];
  }
  getEntriesByType(type) {
    return [];
  }
};
var Performance = class {
  static {
    __name(this, "Performance");
  }
  __unenv__ = true;
  timeOrigin = _timeOrigin;
  eventCounts = /* @__PURE__ */ new Map();
  _entries = [];
  _resourceTimingBufferSize = 0;
  navigation = void 0;
  timing = void 0;
  timerify(_fn, _options) {
    throw createNotImplementedError("Performance.timerify");
  }
  get nodeTiming() {
    return nodeTiming;
  }
  eventLoopUtilization() {
    return {};
  }
  markResourceTiming() {
    return new PerformanceResourceTiming("");
  }
  onresourcetimingbufferfull = null;
  now() {
    if (this.timeOrigin === _timeOrigin) {
      return _performanceNow();
    }
    return Date.now() - this.timeOrigin;
  }
  clearMarks(markName) {
    this._entries = markName ? this._entries.filter((e) => e.name !== markName) : this._entries.filter((e) => e.entryType !== "mark");
  }
  clearMeasures(measureName) {
    this._entries = measureName ? this._entries.filter((e) => e.name !== measureName) : this._entries.filter((e) => e.entryType !== "measure");
  }
  clearResourceTimings() {
    this._entries = this._entries.filter((e) => e.entryType !== "resource" || e.entryType !== "navigation");
  }
  getEntries() {
    return this._entries;
  }
  getEntriesByName(name, type) {
    return this._entries.filter((e) => e.name === name && (!type || e.entryType === type));
  }
  getEntriesByType(type) {
    return this._entries.filter((e) => e.entryType === type);
  }
  mark(name, options) {
    const entry = new PerformanceMark(name, options);
    this._entries.push(entry);
    return entry;
  }
  measure(measureName, startOrMeasureOptions, endMark) {
    let start;
    let end;
    if (typeof startOrMeasureOptions === "string") {
      start = this.getEntriesByName(startOrMeasureOptions, "mark")[0]?.startTime;
      end = this.getEntriesByName(endMark, "mark")[0]?.startTime;
    } else {
      start = Number.parseFloat(startOrMeasureOptions?.start) || this.now();
      end = Number.parseFloat(startOrMeasureOptions?.end) || this.now();
    }
    const entry = new PerformanceMeasure(measureName, {
      startTime: start,
      detail: {
        start,
        end
      }
    });
    this._entries.push(entry);
    return entry;
  }
  setResourceTimingBufferSize(maxSize) {
    this._resourceTimingBufferSize = maxSize;
  }
  addEventListener(type, listener, options) {
    throw createNotImplementedError("Performance.addEventListener");
  }
  removeEventListener(type, listener, options) {
    throw createNotImplementedError("Performance.removeEventListener");
  }
  dispatchEvent(event) {
    throw createNotImplementedError("Performance.dispatchEvent");
  }
  toJSON() {
    return this;
  }
};
var PerformanceObserver = class {
  static {
    __name(this, "PerformanceObserver");
  }
  __unenv__ = true;
  static supportedEntryTypes = [];
  _callback = null;
  constructor(callback) {
    this._callback = callback;
  }
  takeRecords() {
    return [];
  }
  disconnect() {
    throw createNotImplementedError("PerformanceObserver.disconnect");
  }
  observe(options) {
    throw createNotImplementedError("PerformanceObserver.observe");
  }
  bind(fn) {
    return fn;
  }
  runInAsyncScope(fn, thisArg, ...args) {
    return fn.call(thisArg, ...args);
  }
  asyncId() {
    return 0;
  }
  triggerAsyncId() {
    return 0;
  }
  emitDestroy() {
    return this;
  }
};
var performance = globalThis.performance && "addEventListener" in globalThis.performance ? globalThis.performance : new Performance();

// node_modules/@cloudflare/unenv-preset/dist/runtime/polyfill/performance.mjs
if (!("__unenv__" in performance)) {
  const proto = Performance.prototype;
  for (const key of Object.getOwnPropertyNames(proto)) {
    if (key !== "constructor" && !(key in performance)) {
      const desc = Object.getOwnPropertyDescriptor(proto, key);
      if (desc) {
        Object.defineProperty(performance, key, desc);
      }
    }
  }
}
globalThis.performance = performance;
globalThis.Performance = Performance;
globalThis.PerformanceEntry = PerformanceEntry;
globalThis.PerformanceMark = PerformanceMark;
globalThis.PerformanceMeasure = PerformanceMeasure;
globalThis.PerformanceObserver = PerformanceObserver;
globalThis.PerformanceObserverEntryList = PerformanceObserverEntryList;
globalThis.PerformanceResourceTiming = PerformanceResourceTiming;

// node_modules/unenv/dist/runtime/node/internal/process/hrtime.mjs
var hrtime = /* @__PURE__ */ Object.assign(/* @__PURE__ */ __name(function hrtime2(startTime) {
  const now = Date.now();
  const seconds = Math.trunc(now / 1e3);
  const nanos = now % 1e3 * 1e6;
  if (startTime) {
    let diffSeconds = seconds - startTime[0];
    let diffNanos = nanos - startTime[0];
    if (diffNanos < 0) {
      diffSeconds = diffSeconds - 1;
      diffNanos = 1e9 + diffNanos;
    }
    return [diffSeconds, diffNanos];
  }
  return [seconds, nanos];
}, "hrtime"), { bigint: /* @__PURE__ */ __name(function bigint() {
  return BigInt(Date.now() * 1e6);
}, "bigint") });

// node_modules/unenv/dist/runtime/node/internal/process/process.mjs
import { EventEmitter } from "node:events";

// node_modules/unenv/dist/runtime/node/internal/tty/read-stream.mjs
var ReadStream = class {
  static {
    __name(this, "ReadStream");
  }
  fd;
  isRaw = false;
  isTTY = false;
  constructor(fd) {
    this.fd = fd;
  }
  setRawMode(mode) {
    this.isRaw = mode;
    return this;
  }
};

// node_modules/unenv/dist/runtime/node/internal/tty/write-stream.mjs
var WriteStream = class {
  static {
    __name(this, "WriteStream");
  }
  fd;
  columns = 80;
  rows = 24;
  isTTY = false;
  constructor(fd) {
    this.fd = fd;
  }
  clearLine(dir, callback) {
    callback && callback();
    return false;
  }
  clearScreenDown(callback) {
    callback && callback();
    return false;
  }
  cursorTo(x, y, callback) {
    callback && typeof callback === "function" && callback();
    return false;
  }
  moveCursor(dx, dy, callback) {
    callback && callback();
    return false;
  }
  getColorDepth(env2) {
    return 1;
  }
  hasColors(count, env2) {
    return false;
  }
  getWindowSize() {
    return [this.columns, this.rows];
  }
  write(str, encoding, cb) {
    if (str instanceof Uint8Array) {
      str = new TextDecoder().decode(str);
    }
    try {
      console.log(str);
    } catch {
    }
    cb && typeof cb === "function" && cb();
    return false;
  }
};

// node_modules/unenv/dist/runtime/node/internal/process/node-version.mjs
var NODE_VERSION = "22.14.0";

// node_modules/unenv/dist/runtime/node/internal/process/process.mjs
var Process = class _Process extends EventEmitter {
  static {
    __name(this, "Process");
  }
  env;
  hrtime;
  nextTick;
  constructor(impl) {
    super();
    this.env = impl.env;
    this.hrtime = impl.hrtime;
    this.nextTick = impl.nextTick;
    for (const prop of [...Object.getOwnPropertyNames(_Process.prototype), ...Object.getOwnPropertyNames(EventEmitter.prototype)]) {
      const value = this[prop];
      if (typeof value === "function") {
        this[prop] = value.bind(this);
      }
    }
  }
  // --- event emitter ---
  emitWarning(warning, type, code) {
    console.warn(`${code ? `[${code}] ` : ""}${type ? `${type}: ` : ""}${warning}`);
  }
  emit(...args) {
    return super.emit(...args);
  }
  listeners(eventName) {
    return super.listeners(eventName);
  }
  // --- stdio (lazy initializers) ---
  #stdin;
  #stdout;
  #stderr;
  get stdin() {
    return this.#stdin ??= new ReadStream(0);
  }
  get stdout() {
    return this.#stdout ??= new WriteStream(1);
  }
  get stderr() {
    return this.#stderr ??= new WriteStream(2);
  }
  // --- cwd ---
  #cwd = "/";
  chdir(cwd2) {
    this.#cwd = cwd2;
  }
  cwd() {
    return this.#cwd;
  }
  // --- dummy props and getters ---
  arch = "";
  platform = "";
  argv = [];
  argv0 = "";
  execArgv = [];
  execPath = "";
  title = "";
  pid = 200;
  ppid = 100;
  get version() {
    return `v${NODE_VERSION}`;
  }
  get versions() {
    return { node: NODE_VERSION };
  }
  get allowedNodeEnvironmentFlags() {
    return /* @__PURE__ */ new Set();
  }
  get sourceMapsEnabled() {
    return false;
  }
  get debugPort() {
    return 0;
  }
  get throwDeprecation() {
    return false;
  }
  get traceDeprecation() {
    return false;
  }
  get features() {
    return {};
  }
  get release() {
    return {};
  }
  get connected() {
    return false;
  }
  get config() {
    return {};
  }
  get moduleLoadList() {
    return [];
  }
  constrainedMemory() {
    return 0;
  }
  availableMemory() {
    return 0;
  }
  uptime() {
    return 0;
  }
  resourceUsage() {
    return {};
  }
  // --- noop methods ---
  ref() {
  }
  unref() {
  }
  // --- unimplemented methods ---
  umask() {
    throw createNotImplementedError("process.umask");
  }
  getBuiltinModule() {
    return void 0;
  }
  getActiveResourcesInfo() {
    throw createNotImplementedError("process.getActiveResourcesInfo");
  }
  exit() {
    throw createNotImplementedError("process.exit");
  }
  reallyExit() {
    throw createNotImplementedError("process.reallyExit");
  }
  kill() {
    throw createNotImplementedError("process.kill");
  }
  abort() {
    throw createNotImplementedError("process.abort");
  }
  dlopen() {
    throw createNotImplementedError("process.dlopen");
  }
  setSourceMapsEnabled() {
    throw createNotImplementedError("process.setSourceMapsEnabled");
  }
  loadEnvFile() {
    throw createNotImplementedError("process.loadEnvFile");
  }
  disconnect() {
    throw createNotImplementedError("process.disconnect");
  }
  cpuUsage() {
    throw createNotImplementedError("process.cpuUsage");
  }
  setUncaughtExceptionCaptureCallback() {
    throw createNotImplementedError("process.setUncaughtExceptionCaptureCallback");
  }
  hasUncaughtExceptionCaptureCallback() {
    throw createNotImplementedError("process.hasUncaughtExceptionCaptureCallback");
  }
  initgroups() {
    throw createNotImplementedError("process.initgroups");
  }
  openStdin() {
    throw createNotImplementedError("process.openStdin");
  }
  assert() {
    throw createNotImplementedError("process.assert");
  }
  binding() {
    throw createNotImplementedError("process.binding");
  }
  // --- attached interfaces ---
  permission = { has: /* @__PURE__ */ notImplemented("process.permission.has") };
  report = {
    directory: "",
    filename: "",
    signal: "SIGUSR2",
    compact: false,
    reportOnFatalError: false,
    reportOnSignal: false,
    reportOnUncaughtException: false,
    getReport: /* @__PURE__ */ notImplemented("process.report.getReport"),
    writeReport: /* @__PURE__ */ notImplemented("process.report.writeReport")
  };
  finalization = {
    register: /* @__PURE__ */ notImplemented("process.finalization.register"),
    unregister: /* @__PURE__ */ notImplemented("process.finalization.unregister"),
    registerBeforeExit: /* @__PURE__ */ notImplemented("process.finalization.registerBeforeExit")
  };
  memoryUsage = Object.assign(() => ({
    arrayBuffers: 0,
    rss: 0,
    external: 0,
    heapTotal: 0,
    heapUsed: 0
  }), { rss: /* @__PURE__ */ __name(() => 0, "rss") });
  // --- undefined props ---
  mainModule = void 0;
  domain = void 0;
  // optional
  send = void 0;
  exitCode = void 0;
  channel = void 0;
  getegid = void 0;
  geteuid = void 0;
  getgid = void 0;
  getgroups = void 0;
  getuid = void 0;
  setegid = void 0;
  seteuid = void 0;
  setgid = void 0;
  setgroups = void 0;
  setuid = void 0;
  // internals
  _events = void 0;
  _eventsCount = void 0;
  _exiting = void 0;
  _maxListeners = void 0;
  _debugEnd = void 0;
  _debugProcess = void 0;
  _fatalException = void 0;
  _getActiveHandles = void 0;
  _getActiveRequests = void 0;
  _kill = void 0;
  _preload_modules = void 0;
  _rawDebug = void 0;
  _startProfilerIdleNotifier = void 0;
  _stopProfilerIdleNotifier = void 0;
  _tickCallback = void 0;
  _disconnect = void 0;
  _handleQueue = void 0;
  _pendingMessage = void 0;
  _channel = void 0;
  _send = void 0;
  _linkedBinding = void 0;
};

// node_modules/@cloudflare/unenv-preset/dist/runtime/node/process.mjs
var globalProcess = globalThis["process"];
var getBuiltinModule = globalProcess.getBuiltinModule;
var workerdProcess = getBuiltinModule("node:process");
var unenvProcess = new Process({
  env: globalProcess.env,
  hrtime,
  // `nextTick` is available from workerd process v1
  nextTick: workerdProcess.nextTick
});
var { exit, features, platform } = workerdProcess;
var {
  _channel,
  _debugEnd,
  _debugProcess,
  _disconnect,
  _events,
  _eventsCount,
  _exiting,
  _fatalException,
  _getActiveHandles,
  _getActiveRequests,
  _handleQueue,
  _kill,
  _linkedBinding,
  _maxListeners,
  _pendingMessage,
  _preload_modules,
  _rawDebug,
  _send,
  _startProfilerIdleNotifier,
  _stopProfilerIdleNotifier,
  _tickCallback,
  abort,
  addListener,
  allowedNodeEnvironmentFlags,
  arch,
  argv,
  argv0,
  assert,
  availableMemory,
  binding,
  channel,
  chdir,
  config,
  connected,
  constrainedMemory,
  cpuUsage,
  cwd,
  debugPort,
  disconnect,
  dlopen,
  domain,
  emit,
  emitWarning,
  env,
  eventNames,
  execArgv,
  execPath,
  exitCode,
  finalization,
  getActiveResourcesInfo,
  getegid,
  geteuid,
  getgid,
  getgroups,
  getMaxListeners,
  getuid,
  hasUncaughtExceptionCaptureCallback,
  hrtime: hrtime3,
  initgroups,
  kill,
  listenerCount,
  listeners,
  loadEnvFile,
  mainModule,
  memoryUsage,
  moduleLoadList,
  nextTick,
  off,
  on,
  once,
  openStdin,
  permission,
  pid,
  ppid,
  prependListener,
  prependOnceListener,
  rawListeners,
  reallyExit,
  ref,
  release,
  removeAllListeners,
  removeListener,
  report,
  resourceUsage,
  send,
  setegid,
  seteuid,
  setgid,
  setgroups,
  setMaxListeners,
  setSourceMapsEnabled,
  setuid,
  setUncaughtExceptionCaptureCallback,
  sourceMapsEnabled,
  stderr,
  stdin,
  stdout,
  throwDeprecation,
  title,
  traceDeprecation,
  umask,
  unref,
  uptime,
  version,
  versions
} = unenvProcess;
var _process = {
  abort,
  addListener,
  allowedNodeEnvironmentFlags,
  hasUncaughtExceptionCaptureCallback,
  setUncaughtExceptionCaptureCallback,
  loadEnvFile,
  sourceMapsEnabled,
  arch,
  argv,
  argv0,
  chdir,
  config,
  connected,
  constrainedMemory,
  availableMemory,
  cpuUsage,
  cwd,
  debugPort,
  dlopen,
  disconnect,
  emit,
  emitWarning,
  env,
  eventNames,
  execArgv,
  execPath,
  exit,
  finalization,
  features,
  getBuiltinModule,
  getActiveResourcesInfo,
  getMaxListeners,
  hrtime: hrtime3,
  kill,
  listeners,
  listenerCount,
  memoryUsage,
  nextTick,
  on,
  off,
  once,
  pid,
  platform,
  ppid,
  prependListener,
  prependOnceListener,
  rawListeners,
  release,
  removeAllListeners,
  removeListener,
  report,
  resourceUsage,
  setMaxListeners,
  setSourceMapsEnabled,
  stderr,
  stdin,
  stdout,
  title,
  throwDeprecation,
  traceDeprecation,
  umask,
  uptime,
  version,
  versions,
  // @ts-expect-error old API
  domain,
  initgroups,
  moduleLoadList,
  reallyExit,
  openStdin,
  assert,
  binding,
  send,
  exitCode,
  channel,
  getegid,
  geteuid,
  getgid,
  getgroups,
  getuid,
  setegid,
  seteuid,
  setgid,
  setgroups,
  setuid,
  permission,
  mainModule,
  _events,
  _eventsCount,
  _exiting,
  _maxListeners,
  _debugEnd,
  _debugProcess,
  _fatalException,
  _getActiveHandles,
  _getActiveRequests,
  _kill,
  _preload_modules,
  _rawDebug,
  _startProfilerIdleNotifier,
  _stopProfilerIdleNotifier,
  _tickCallback,
  _disconnect,
  _handleQueue,
  _pendingMessage,
  _channel,
  _send,
  _linkedBinding
};
var process_default = _process;

// node_modules/wrangler/_virtual_unenv_global_polyfill-@cloudflare-unenv-preset-node-process
globalThis.process = process_default;

// node_modules/hono/dist/compose.js
var compose = /* @__PURE__ */ __name((middleware, onError, onNotFound) => {
  return (context, next) => {
    let index = -1;
    return dispatch(0);
    async function dispatch(i) {
      if (i <= index) {
        throw new Error("next() called multiple times");
      }
      index = i;
      let res;
      let isError = false;
      let handler;
      if (middleware[i]) {
        handler = middleware[i][0][0];
        context.req.routeIndex = i;
      } else {
        handler = i === middleware.length && next || void 0;
      }
      if (handler) {
        try {
          res = await handler(context, () => dispatch(i + 1));
        } catch (err) {
          if (err instanceof Error && onError) {
            context.error = err;
            res = await onError(err, context);
            isError = true;
          } else {
            throw err;
          }
        }
      } else {
        if (context.finalized === false && onNotFound) {
          res = await onNotFound(context);
        }
      }
      if (res && (context.finalized === false || isError)) {
        context.res = res;
      }
      return context;
    }
    __name(dispatch, "dispatch");
  };
}, "compose");

// node_modules/hono/dist/request/constants.js
var GET_MATCH_RESULT = /* @__PURE__ */ Symbol();

// node_modules/hono/dist/utils/body.js
var parseBody = /* @__PURE__ */ __name(async (request, options = /* @__PURE__ */ Object.create(null)) => {
  const { all = false, dot = false } = options;
  const headers = request instanceof HonoRequest ? request.raw.headers : request.headers;
  const contentType = headers.get("Content-Type");
  if (contentType?.startsWith("multipart/form-data") || contentType?.startsWith("application/x-www-form-urlencoded")) {
    return parseFormData(request, { all, dot });
  }
  return {};
}, "parseBody");
async function parseFormData(request, options) {
  const formData = await request.formData();
  if (formData) {
    return convertFormDataToBodyData(formData, options);
  }
  return {};
}
__name(parseFormData, "parseFormData");
function convertFormDataToBodyData(formData, options) {
  const form = /* @__PURE__ */ Object.create(null);
  formData.forEach((value, key) => {
    const shouldParseAllValues = options.all || key.endsWith("[]");
    if (!shouldParseAllValues) {
      form[key] = value;
    } else {
      handleParsingAllValues(form, key, value);
    }
  });
  if (options.dot) {
    Object.entries(form).forEach(([key, value]) => {
      const shouldParseDotValues = key.includes(".");
      if (shouldParseDotValues) {
        handleParsingNestedValues(form, key, value);
        delete form[key];
      }
    });
  }
  return form;
}
__name(convertFormDataToBodyData, "convertFormDataToBodyData");
var handleParsingAllValues = /* @__PURE__ */ __name((form, key, value) => {
  if (form[key] !== void 0) {
    if (Array.isArray(form[key])) {
      ;
      form[key].push(value);
    } else {
      form[key] = [form[key], value];
    }
  } else {
    if (!key.endsWith("[]")) {
      form[key] = value;
    } else {
      form[key] = [value];
    }
  }
}, "handleParsingAllValues");
var handleParsingNestedValues = /* @__PURE__ */ __name((form, key, value) => {
  if (/(?:^|\.)__proto__\./.test(key)) {
    return;
  }
  let nestedForm = form;
  const keys = key.split(".");
  keys.forEach((key2, index) => {
    if (index === keys.length - 1) {
      nestedForm[key2] = value;
    } else {
      if (!nestedForm[key2] || typeof nestedForm[key2] !== "object" || Array.isArray(nestedForm[key2]) || nestedForm[key2] instanceof File) {
        nestedForm[key2] = /* @__PURE__ */ Object.create(null);
      }
      nestedForm = nestedForm[key2];
    }
  });
}, "handleParsingNestedValues");

// node_modules/hono/dist/utils/url.js
var splitPath = /* @__PURE__ */ __name((path) => {
  const paths = path.split("/");
  if (paths[0] === "") {
    paths.shift();
  }
  return paths;
}, "splitPath");
var splitRoutingPath = /* @__PURE__ */ __name((routePath) => {
  const { groups, path } = extractGroupsFromPath(routePath);
  const paths = splitPath(path);
  return replaceGroupMarks(paths, groups);
}, "splitRoutingPath");
var extractGroupsFromPath = /* @__PURE__ */ __name((path) => {
  const groups = [];
  path = path.replace(/\{[^}]+\}/g, (match2, index) => {
    const mark = `@${index}`;
    groups.push([mark, match2]);
    return mark;
  });
  return { groups, path };
}, "extractGroupsFromPath");
var replaceGroupMarks = /* @__PURE__ */ __name((paths, groups) => {
  for (let i = groups.length - 1; i >= 0; i--) {
    const [mark] = groups[i];
    for (let j = paths.length - 1; j >= 0; j--) {
      if (paths[j].includes(mark)) {
        paths[j] = paths[j].replace(mark, groups[i][1]);
        break;
      }
    }
  }
  return paths;
}, "replaceGroupMarks");
var patternCache = {};
var getPattern = /* @__PURE__ */ __name((label, next) => {
  if (label === "*") {
    return "*";
  }
  const match2 = label.match(/^\:([^\{\}]+)(?:\{(.+)\})?$/);
  if (match2) {
    const cacheKey = `${label}#${next}`;
    if (!patternCache[cacheKey]) {
      if (match2[2]) {
        patternCache[cacheKey] = next && next[0] !== ":" && next[0] !== "*" ? [cacheKey, match2[1], new RegExp(`^${match2[2]}(?=/${next})`)] : [label, match2[1], new RegExp(`^${match2[2]}$`)];
      } else {
        patternCache[cacheKey] = [label, match2[1], true];
      }
    }
    return patternCache[cacheKey];
  }
  return null;
}, "getPattern");
var tryDecode = /* @__PURE__ */ __name((str, decoder) => {
  try {
    return decoder(str);
  } catch {
    return str.replace(/(?:%[0-9A-Fa-f]{2})+/g, (match2) => {
      try {
        return decoder(match2);
      } catch {
        return match2;
      }
    });
  }
}, "tryDecode");
var tryDecodeURI = /* @__PURE__ */ __name((str) => tryDecode(str, decodeURI), "tryDecodeURI");
var getPath = /* @__PURE__ */ __name((request) => {
  const url = request.url;
  const start = url.indexOf("/", url.indexOf(":") + 4);
  let i = start;
  for (; i < url.length; i++) {
    const charCode = url.charCodeAt(i);
    if (charCode === 37) {
      const queryIndex = url.indexOf("?", i);
      const hashIndex = url.indexOf("#", i);
      const end = queryIndex === -1 ? hashIndex === -1 ? void 0 : hashIndex : hashIndex === -1 ? queryIndex : Math.min(queryIndex, hashIndex);
      const path = url.slice(start, end);
      return tryDecodeURI(path.includes("%25") ? path.replace(/%25/g, "%2525") : path);
    } else if (charCode === 63 || charCode === 35) {
      break;
    }
  }
  return url.slice(start, i);
}, "getPath");
var getPathNoStrict = /* @__PURE__ */ __name((request) => {
  const result = getPath(request);
  return result.length > 1 && result.at(-1) === "/" ? result.slice(0, -1) : result;
}, "getPathNoStrict");
var mergePath = /* @__PURE__ */ __name((base, sub, ...rest) => {
  if (rest.length) {
    sub = mergePath(sub, ...rest);
  }
  return `${base?.[0] === "/" ? "" : "/"}${base}${sub === "/" ? "" : `${base?.at(-1) === "/" ? "" : "/"}${sub?.[0] === "/" ? sub.slice(1) : sub}`}`;
}, "mergePath");
var checkOptionalParameter = /* @__PURE__ */ __name((path) => {
  if (path.charCodeAt(path.length - 1) !== 63 || !path.includes(":")) {
    return null;
  }
  const segments = path.split("/");
  const results = [];
  let basePath = "";
  segments.forEach((segment) => {
    if (segment !== "" && !/\:/.test(segment)) {
      basePath += "/" + segment;
    } else if (/\:/.test(segment)) {
      if (/\?/.test(segment)) {
        if (results.length === 0 && basePath === "") {
          results.push("/");
        } else {
          results.push(basePath);
        }
        const optionalSegment = segment.replace("?", "");
        basePath += "/" + optionalSegment;
        results.push(basePath);
      } else {
        basePath += "/" + segment;
      }
    }
  });
  return results.filter((v, i, a) => a.indexOf(v) === i);
}, "checkOptionalParameter");
var _decodeURI = /* @__PURE__ */ __name((value) => {
  if (!/[%+]/.test(value)) {
    return value;
  }
  if (value.indexOf("+") !== -1) {
    value = value.replace(/\+/g, " ");
  }
  return value.indexOf("%") !== -1 ? tryDecode(value, decodeURIComponent_) : value;
}, "_decodeURI");
var _getQueryParam = /* @__PURE__ */ __name((url, key, multiple) => {
  let encoded;
  if (!multiple && key && !/[%+]/.test(key)) {
    let keyIndex2 = url.indexOf("?", 8);
    if (keyIndex2 === -1) {
      return void 0;
    }
    if (!url.startsWith(key, keyIndex2 + 1)) {
      keyIndex2 = url.indexOf(`&${key}`, keyIndex2 + 1);
    }
    while (keyIndex2 !== -1) {
      const trailingKeyCode = url.charCodeAt(keyIndex2 + key.length + 1);
      if (trailingKeyCode === 61) {
        const valueIndex = keyIndex2 + key.length + 2;
        const endIndex = url.indexOf("&", valueIndex);
        return _decodeURI(url.slice(valueIndex, endIndex === -1 ? void 0 : endIndex));
      } else if (trailingKeyCode == 38 || isNaN(trailingKeyCode)) {
        return "";
      }
      keyIndex2 = url.indexOf(`&${key}`, keyIndex2 + 1);
    }
    encoded = /[%+]/.test(url);
    if (!encoded) {
      return void 0;
    }
  }
  const results = {};
  encoded ??= /[%+]/.test(url);
  let keyIndex = url.indexOf("?", 8);
  while (keyIndex !== -1) {
    const nextKeyIndex = url.indexOf("&", keyIndex + 1);
    let valueIndex = url.indexOf("=", keyIndex);
    if (valueIndex > nextKeyIndex && nextKeyIndex !== -1) {
      valueIndex = -1;
    }
    let name = url.slice(
      keyIndex + 1,
      valueIndex === -1 ? nextKeyIndex === -1 ? void 0 : nextKeyIndex : valueIndex
    );
    if (encoded) {
      name = _decodeURI(name);
    }
    keyIndex = nextKeyIndex;
    if (name === "") {
      continue;
    }
    let value;
    if (valueIndex === -1) {
      value = "";
    } else {
      value = url.slice(valueIndex + 1, nextKeyIndex === -1 ? void 0 : nextKeyIndex);
      if (encoded) {
        value = _decodeURI(value);
      }
    }
    if (multiple) {
      if (!(results[name] && Array.isArray(results[name]))) {
        results[name] = [];
      }
      ;
      results[name].push(value);
    } else {
      results[name] ??= value;
    }
  }
  return key ? results[key] : results;
}, "_getQueryParam");
var getQueryParam = _getQueryParam;
var getQueryParams = /* @__PURE__ */ __name((url, key) => {
  return _getQueryParam(url, key, true);
}, "getQueryParams");
var decodeURIComponent_ = decodeURIComponent;

// node_modules/hono/dist/request.js
var tryDecodeURIComponent = /* @__PURE__ */ __name((str) => tryDecode(str, decodeURIComponent_), "tryDecodeURIComponent");
var HonoRequest = class {
  static {
    __name(this, "HonoRequest");
  }
  /**
   * `.raw` can get the raw Request object.
   *
   * @see {@link https://hono.dev/docs/api/request#raw}
   *
   * @example
   * ```ts
   * // For Cloudflare Workers
   * app.post('/', async (c) => {
   *   const metadata = c.req.raw.cf?.hostMetadata?
   *   ...
   * })
   * ```
   */
  raw;
  #validatedData;
  // Short name of validatedData
  #matchResult;
  routeIndex = 0;
  /**
   * `.path` can get the pathname of the request.
   *
   * @see {@link https://hono.dev/docs/api/request#path}
   *
   * @example
   * ```ts
   * app.get('/about/me', (c) => {
   *   const pathname = c.req.path // `/about/me`
   * })
   * ```
   */
  path;
  bodyCache = {};
  constructor(request, path = "/", matchResult = [[]]) {
    this.raw = request;
    this.path = path;
    this.#matchResult = matchResult;
    this.#validatedData = {};
  }
  param(key) {
    return key ? this.#getDecodedParam(key) : this.#getAllDecodedParams();
  }
  #getDecodedParam(key) {
    const paramKey = this.#matchResult[0][this.routeIndex][1][key];
    const param = this.#getParamValue(paramKey);
    return param && /\%/.test(param) ? tryDecodeURIComponent(param) : param;
  }
  #getAllDecodedParams() {
    const decoded = {};
    const keys = Object.keys(this.#matchResult[0][this.routeIndex][1]);
    for (const key of keys) {
      const value = this.#getParamValue(this.#matchResult[0][this.routeIndex][1][key]);
      if (value !== void 0) {
        decoded[key] = /\%/.test(value) ? tryDecodeURIComponent(value) : value;
      }
    }
    return decoded;
  }
  #getParamValue(paramKey) {
    return this.#matchResult[1] ? this.#matchResult[1][paramKey] : paramKey;
  }
  query(key) {
    return getQueryParam(this.url, key);
  }
  queries(key) {
    return getQueryParams(this.url, key);
  }
  header(name) {
    if (name) {
      return this.raw.headers.get(name) ?? void 0;
    }
    const headerData = {};
    this.raw.headers.forEach((value, key) => {
      headerData[key] = value;
    });
    return headerData;
  }
  async parseBody(options) {
    return parseBody(this, options);
  }
  #cachedBody = /* @__PURE__ */ __name((key) => {
    const { bodyCache, raw: raw2 } = this;
    const cachedBody = bodyCache[key];
    if (cachedBody) {
      return cachedBody;
    }
    const anyCachedKey = Object.keys(bodyCache)[0];
    if (anyCachedKey) {
      return bodyCache[anyCachedKey].then((body) => {
        if (anyCachedKey === "json") {
          body = JSON.stringify(body);
        }
        return new Response(body)[key]();
      });
    }
    return bodyCache[key] = raw2[key]();
  }, "#cachedBody");
  /**
   * `.json()` can parse Request body of type `application/json`
   *
   * @see {@link https://hono.dev/docs/api/request#json}
   *
   * @example
   * ```ts
   * app.post('/entry', async (c) => {
   *   const body = await c.req.json()
   * })
   * ```
   */
  json() {
    return this.#cachedBody("text").then((text) => JSON.parse(text));
  }
  /**
   * `.text()` can parse Request body of type `text/plain`
   *
   * @see {@link https://hono.dev/docs/api/request#text}
   *
   * @example
   * ```ts
   * app.post('/entry', async (c) => {
   *   const body = await c.req.text()
   * })
   * ```
   */
  text() {
    return this.#cachedBody("text");
  }
  /**
   * `.arrayBuffer()` parse Request body as an `ArrayBuffer`
   *
   * @see {@link https://hono.dev/docs/api/request#arraybuffer}
   *
   * @example
   * ```ts
   * app.post('/entry', async (c) => {
   *   const body = await c.req.arrayBuffer()
   * })
   * ```
   */
  arrayBuffer() {
    return this.#cachedBody("arrayBuffer");
  }
  /**
   * Parses the request body as a `Blob`.
   * @example
   * ```ts
   * app.post('/entry', async (c) => {
   *   const body = await c.req.blob();
   * });
   * ```
   * @see https://hono.dev/docs/api/request#blob
   */
  blob() {
    return this.#cachedBody("blob");
  }
  /**
   * Parses the request body as `FormData`.
   * @example
   * ```ts
   * app.post('/entry', async (c) => {
   *   const body = await c.req.formData();
   * });
   * ```
   * @see https://hono.dev/docs/api/request#formdata
   */
  formData() {
    return this.#cachedBody("formData");
  }
  /**
   * Adds validated data to the request.
   *
   * @param target - The target of the validation.
   * @param data - The validated data to add.
   */
  addValidatedData(target, data) {
    this.#validatedData[target] = data;
  }
  valid(target) {
    return this.#validatedData[target];
  }
  /**
   * `.url()` can get the request url strings.
   *
   * @see {@link https://hono.dev/docs/api/request#url}
   *
   * @example
   * ```ts
   * app.get('/about/me', (c) => {
   *   const url = c.req.url // `http://localhost:8787/about/me`
   *   ...
   * })
   * ```
   */
  get url() {
    return this.raw.url;
  }
  /**
   * `.method()` can get the method name of the request.
   *
   * @see {@link https://hono.dev/docs/api/request#method}
   *
   * @example
   * ```ts
   * app.get('/about/me', (c) => {
   *   const method = c.req.method // `GET`
   * })
   * ```
   */
  get method() {
    return this.raw.method;
  }
  get [GET_MATCH_RESULT]() {
    return this.#matchResult;
  }
  /**
   * `.matchedRoutes()` can return a matched route in the handler
   *
   * @deprecated
   *
   * Use matchedRoutes helper defined in "hono/route" instead.
   *
   * @see {@link https://hono.dev/docs/api/request#matchedroutes}
   *
   * @example
   * ```ts
   * app.use('*', async function logger(c, next) {
   *   await next()
   *   c.req.matchedRoutes.forEach(({ handler, method, path }, i) => {
   *     const name = handler.name || (handler.length < 2 ? '[handler]' : '[middleware]')
   *     console.log(
   *       method,
   *       ' ',
   *       path,
   *       ' '.repeat(Math.max(10 - path.length, 0)),
   *       name,
   *       i === c.req.routeIndex ? '<- respond from here' : ''
   *     )
   *   })
   * })
   * ```
   */
  get matchedRoutes() {
    return this.#matchResult[0].map(([[, route]]) => route);
  }
  /**
   * `routePath()` can retrieve the path registered within the handler
   *
   * @deprecated
   *
   * Use routePath helper defined in "hono/route" instead.
   *
   * @see {@link https://hono.dev/docs/api/request#routepath}
   *
   * @example
   * ```ts
   * app.get('/posts/:id', (c) => {
   *   return c.json({ path: c.req.routePath })
   * })
   * ```
   */
  get routePath() {
    return this.#matchResult[0].map(([[, route]]) => route)[this.routeIndex].path;
  }
};

// node_modules/hono/dist/utils/html.js
var HtmlEscapedCallbackPhase = {
  Stringify: 1,
  BeforeStream: 2,
  Stream: 3
};
var raw = /* @__PURE__ */ __name((value, callbacks) => {
  const escapedString = new String(value);
  escapedString.isEscaped = true;
  escapedString.callbacks = callbacks;
  return escapedString;
}, "raw");
var resolveCallback = /* @__PURE__ */ __name(async (str, phase, preserveCallbacks, context, buffer) => {
  if (typeof str === "object" && !(str instanceof String)) {
    if (!(str instanceof Promise)) {
      str = str.toString();
    }
    if (str instanceof Promise) {
      str = await str;
    }
  }
  const callbacks = str.callbacks;
  if (!callbacks?.length) {
    return Promise.resolve(str);
  }
  if (buffer) {
    buffer[0] += str;
  } else {
    buffer = [str];
  }
  const resStr = Promise.all(callbacks.map((c) => c({ phase, buffer, context }))).then(
    (res) => Promise.all(
      res.filter(Boolean).map((str2) => resolveCallback(str2, phase, false, context, buffer))
    ).then(() => buffer[0])
  );
  if (preserveCallbacks) {
    return raw(await resStr, callbacks);
  } else {
    return resStr;
  }
}, "resolveCallback");

// node_modules/hono/dist/context.js
var TEXT_PLAIN = "text/plain; charset=UTF-8";
var setDefaultContentType = /* @__PURE__ */ __name((contentType, headers) => {
  return {
    "Content-Type": contentType,
    ...headers
  };
}, "setDefaultContentType");
var createResponseInstance = /* @__PURE__ */ __name((body, init) => new Response(body, init), "createResponseInstance");
var Context = class {
  static {
    __name(this, "Context");
  }
  #rawRequest;
  #req;
  /**
   * `.env` can get bindings (environment variables, secrets, KV namespaces, D1 database, R2 bucket etc.) in Cloudflare Workers.
   *
   * @see {@link https://hono.dev/docs/api/context#env}
   *
   * @example
   * ```ts
   * // Environment object for Cloudflare Workers
   * app.get('*', async c => {
   *   const counter = c.env.COUNTER
   * })
   * ```
   */
  env = {};
  #var;
  finalized = false;
  /**
   * `.error` can get the error object from the middleware if the Handler throws an error.
   *
   * @see {@link https://hono.dev/docs/api/context#error}
   *
   * @example
   * ```ts
   * app.use('*', async (c, next) => {
   *   await next()
   *   if (c.error) {
   *     // do something...
   *   }
   * })
   * ```
   */
  error;
  #status;
  #executionCtx;
  #res;
  #layout;
  #renderer;
  #notFoundHandler;
  #preparedHeaders;
  #matchResult;
  #path;
  /**
   * Creates an instance of the Context class.
   *
   * @param req - The Request object.
   * @param options - Optional configuration options for the context.
   */
  constructor(req, options) {
    this.#rawRequest = req;
    if (options) {
      this.#executionCtx = options.executionCtx;
      this.env = options.env;
      this.#notFoundHandler = options.notFoundHandler;
      this.#path = options.path;
      this.#matchResult = options.matchResult;
    }
  }
  /**
   * `.req` is the instance of {@link HonoRequest}.
   */
  get req() {
    this.#req ??= new HonoRequest(this.#rawRequest, this.#path, this.#matchResult);
    return this.#req;
  }
  /**
   * @see {@link https://hono.dev/docs/api/context#event}
   * The FetchEvent associated with the current request.
   *
   * @throws Will throw an error if the context does not have a FetchEvent.
   */
  get event() {
    if (this.#executionCtx && "respondWith" in this.#executionCtx) {
      return this.#executionCtx;
    } else {
      throw Error("This context has no FetchEvent");
    }
  }
  /**
   * @see {@link https://hono.dev/docs/api/context#executionctx}
   * The ExecutionContext associated with the current request.
   *
   * @throws Will throw an error if the context does not have an ExecutionContext.
   */
  get executionCtx() {
    if (this.#executionCtx) {
      return this.#executionCtx;
    } else {
      throw Error("This context has no ExecutionContext");
    }
  }
  /**
   * @see {@link https://hono.dev/docs/api/context#res}
   * The Response object for the current request.
   */
  get res() {
    return this.#res ||= createResponseInstance(null, {
      headers: this.#preparedHeaders ??= new Headers()
    });
  }
  /**
   * Sets the Response object for the current request.
   *
   * @param _res - The Response object to set.
   */
  set res(_res) {
    if (this.#res && _res) {
      _res = createResponseInstance(_res.body, _res);
      for (const [k, v] of this.#res.headers.entries()) {
        if (k === "content-type") {
          continue;
        }
        if (k === "set-cookie") {
          const cookies = this.#res.headers.getSetCookie();
          _res.headers.delete("set-cookie");
          for (const cookie of cookies) {
            _res.headers.append("set-cookie", cookie);
          }
        } else {
          _res.headers.set(k, v);
        }
      }
    }
    this.#res = _res;
    this.finalized = true;
  }
  /**
   * `.render()` can create a response within a layout.
   *
   * @see {@link https://hono.dev/docs/api/context#render-setrenderer}
   *
   * @example
   * ```ts
   * app.get('/', (c) => {
   *   return c.render('Hello!')
   * })
   * ```
   */
  render = /* @__PURE__ */ __name((...args) => {
    this.#renderer ??= (content) => this.html(content);
    return this.#renderer(...args);
  }, "render");
  /**
   * Sets the layout for the response.
   *
   * @param layout - The layout to set.
   * @returns The layout function.
   */
  setLayout = /* @__PURE__ */ __name((layout) => this.#layout = layout, "setLayout");
  /**
   * Gets the current layout for the response.
   *
   * @returns The current layout function.
   */
  getLayout = /* @__PURE__ */ __name(() => this.#layout, "getLayout");
  /**
   * `.setRenderer()` can set the layout in the custom middleware.
   *
   * @see {@link https://hono.dev/docs/api/context#render-setrenderer}
   *
   * @example
   * ```tsx
   * app.use('*', async (c, next) => {
   *   c.setRenderer((content) => {
   *     return c.html(
   *       <html>
   *         <body>
   *           <p>{content}</p>
   *         </body>
   *       </html>
   *     )
   *   })
   *   await next()
   * })
   * ```
   */
  setRenderer = /* @__PURE__ */ __name((renderer) => {
    this.#renderer = renderer;
  }, "setRenderer");
  /**
   * `.header()` can set headers.
   *
   * @see {@link https://hono.dev/docs/api/context#header}
   *
   * @example
   * ```ts
   * app.get('/welcome', (c) => {
   *   // Set headers
   *   c.header('X-Message', 'Hello!')
   *   c.header('Content-Type', 'text/plain')
   *
   *   return c.body('Thank you for coming')
   * })
   * ```
   */
  header = /* @__PURE__ */ __name((name, value, options) => {
    if (this.finalized) {
      this.#res = createResponseInstance(this.#res.body, this.#res);
    }
    const headers = this.#res ? this.#res.headers : this.#preparedHeaders ??= new Headers();
    if (value === void 0) {
      headers.delete(name);
    } else if (options?.append) {
      headers.append(name, value);
    } else {
      headers.set(name, value);
    }
  }, "header");
  status = /* @__PURE__ */ __name((status) => {
    this.#status = status;
  }, "status");
  /**
   * `.set()` can set the value specified by the key.
   *
   * @see {@link https://hono.dev/docs/api/context#set-get}
   *
   * @example
   * ```ts
   * app.use('*', async (c, next) => {
   *   c.set('message', 'Hono is hot!!')
   *   await next()
   * })
   * ```
   */
  set = /* @__PURE__ */ __name((key, value) => {
    this.#var ??= /* @__PURE__ */ new Map();
    this.#var.set(key, value);
  }, "set");
  /**
   * `.get()` can use the value specified by the key.
   *
   * @see {@link https://hono.dev/docs/api/context#set-get}
   *
   * @example
   * ```ts
   * app.get('/', (c) => {
   *   const message = c.get('message')
   *   return c.text(`The message is "${message}"`)
   * })
   * ```
   */
  get = /* @__PURE__ */ __name((key) => {
    return this.#var ? this.#var.get(key) : void 0;
  }, "get");
  /**
   * `.var` can access the value of a variable.
   *
   * @see {@link https://hono.dev/docs/api/context#var}
   *
   * @example
   * ```ts
   * const result = c.var.client.oneMethod()
   * ```
   */
  // c.var.propName is a read-only
  get var() {
    if (!this.#var) {
      return {};
    }
    return Object.fromEntries(this.#var);
  }
  #newResponse(data, arg, headers) {
    const responseHeaders = this.#res ? new Headers(this.#res.headers) : this.#preparedHeaders ?? new Headers();
    if (typeof arg === "object" && "headers" in arg) {
      const argHeaders = arg.headers instanceof Headers ? arg.headers : new Headers(arg.headers);
      for (const [key, value] of argHeaders) {
        if (key.toLowerCase() === "set-cookie") {
          responseHeaders.append(key, value);
        } else {
          responseHeaders.set(key, value);
        }
      }
    }
    if (headers) {
      for (const [k, v] of Object.entries(headers)) {
        if (typeof v === "string") {
          responseHeaders.set(k, v);
        } else {
          responseHeaders.delete(k);
          for (const v2 of v) {
            responseHeaders.append(k, v2);
          }
        }
      }
    }
    const status = typeof arg === "number" ? arg : arg?.status ?? this.#status;
    return createResponseInstance(data, { status, headers: responseHeaders });
  }
  newResponse = /* @__PURE__ */ __name((...args) => this.#newResponse(...args), "newResponse");
  /**
   * `.body()` can return the HTTP response.
   * You can set headers with `.header()` and set HTTP status code with `.status`.
   * This can also be set in `.text()`, `.json()` and so on.
   *
   * @see {@link https://hono.dev/docs/api/context#body}
   *
   * @example
   * ```ts
   * app.get('/welcome', (c) => {
   *   // Set headers
   *   c.header('X-Message', 'Hello!')
   *   c.header('Content-Type', 'text/plain')
   *   // Set HTTP status code
   *   c.status(201)
   *
   *   // Return the response body
   *   return c.body('Thank you for coming')
   * })
   * ```
   */
  body = /* @__PURE__ */ __name((data, arg, headers) => this.#newResponse(data, arg, headers), "body");
  /**
   * `.text()` can render text as `Content-Type:text/plain`.
   *
   * @see {@link https://hono.dev/docs/api/context#text}
   *
   * @example
   * ```ts
   * app.get('/say', (c) => {
   *   return c.text('Hello!')
   * })
   * ```
   */
  text = /* @__PURE__ */ __name((text, arg, headers) => {
    return !this.#preparedHeaders && !this.#status && !arg && !headers && !this.finalized ? new Response(text) : this.#newResponse(
      text,
      arg,
      setDefaultContentType(TEXT_PLAIN, headers)
    );
  }, "text");
  /**
   * `.json()` can render JSON as `Content-Type:application/json`.
   *
   * @see {@link https://hono.dev/docs/api/context#json}
   *
   * @example
   * ```ts
   * app.get('/api', (c) => {
   *   return c.json({ message: 'Hello!' })
   * })
   * ```
   */
  json = /* @__PURE__ */ __name((object, arg, headers) => {
    return this.#newResponse(
      JSON.stringify(object),
      arg,
      setDefaultContentType("application/json", headers)
    );
  }, "json");
  html = /* @__PURE__ */ __name((html, arg, headers) => {
    const res = /* @__PURE__ */ __name((html2) => this.#newResponse(html2, arg, setDefaultContentType("text/html; charset=UTF-8", headers)), "res");
    return typeof html === "object" ? resolveCallback(html, HtmlEscapedCallbackPhase.Stringify, false, {}).then(res) : res(html);
  }, "html");
  /**
   * `.redirect()` can Redirect, default status code is 302.
   *
   * @see {@link https://hono.dev/docs/api/context#redirect}
   *
   * @example
   * ```ts
   * app.get('/redirect', (c) => {
   *   return c.redirect('/')
   * })
   * app.get('/redirect-permanently', (c) => {
   *   return c.redirect('/', 301)
   * })
   * ```
   */
  redirect = /* @__PURE__ */ __name((location, status) => {
    const locationString = String(location);
    this.header(
      "Location",
      // Multibyes should be encoded
      // eslint-disable-next-line no-control-regex
      !/[^\x00-\xFF]/.test(locationString) ? locationString : encodeURI(locationString)
    );
    return this.newResponse(null, status ?? 302);
  }, "redirect");
  /**
   * `.notFound()` can return the Not Found Response.
   *
   * @see {@link https://hono.dev/docs/api/context#notfound}
   *
   * @example
   * ```ts
   * app.get('/notfound', (c) => {
   *   return c.notFound()
   * })
   * ```
   */
  notFound = /* @__PURE__ */ __name(() => {
    this.#notFoundHandler ??= () => createResponseInstance();
    return this.#notFoundHandler(this);
  }, "notFound");
};

// node_modules/hono/dist/router.js
var METHOD_NAME_ALL = "ALL";
var METHOD_NAME_ALL_LOWERCASE = "all";
var METHODS = ["get", "post", "put", "delete", "options", "patch"];
var MESSAGE_MATCHER_IS_ALREADY_BUILT = "Can not add a route since the matcher is already built.";
var UnsupportedPathError = class extends Error {
  static {
    __name(this, "UnsupportedPathError");
  }
};

// node_modules/hono/dist/utils/constants.js
var COMPOSED_HANDLER = "__COMPOSED_HANDLER";

// node_modules/hono/dist/hono-base.js
var notFoundHandler = /* @__PURE__ */ __name((c) => {
  return c.text("404 Not Found", 404);
}, "notFoundHandler");
var errorHandler = /* @__PURE__ */ __name((err, c) => {
  if ("getResponse" in err) {
    const res = err.getResponse();
    return c.newResponse(res.body, res);
  }
  console.error(err);
  return c.text("Internal Server Error", 500);
}, "errorHandler");
var Hono = class _Hono {
  static {
    __name(this, "_Hono");
  }
  get;
  post;
  put;
  delete;
  options;
  patch;
  all;
  on;
  use;
  /*
    This class is like an abstract class and does not have a router.
    To use it, inherit the class and implement router in the constructor.
  */
  router;
  getPath;
  // Cannot use `#` because it requires visibility at JavaScript runtime.
  _basePath = "/";
  #path = "/";
  routes = [];
  constructor(options = {}) {
    const allMethods = [...METHODS, METHOD_NAME_ALL_LOWERCASE];
    allMethods.forEach((method) => {
      this[method] = (args1, ...args) => {
        if (typeof args1 === "string") {
          this.#path = args1;
        } else {
          this.#addRoute(method, this.#path, args1);
        }
        args.forEach((handler) => {
          this.#addRoute(method, this.#path, handler);
        });
        return this;
      };
    });
    this.on = (method, path, ...handlers) => {
      for (const p of [path].flat()) {
        this.#path = p;
        for (const m of [method].flat()) {
          handlers.map((handler) => {
            this.#addRoute(m.toUpperCase(), this.#path, handler);
          });
        }
      }
      return this;
    };
    this.use = (arg1, ...handlers) => {
      if (typeof arg1 === "string") {
        this.#path = arg1;
      } else {
        this.#path = "*";
        handlers.unshift(arg1);
      }
      handlers.forEach((handler) => {
        this.#addRoute(METHOD_NAME_ALL, this.#path, handler);
      });
      return this;
    };
    const { strict, ...optionsWithoutStrict } = options;
    Object.assign(this, optionsWithoutStrict);
    this.getPath = strict ?? true ? options.getPath ?? getPath : getPathNoStrict;
  }
  #clone() {
    const clone = new _Hono({
      router: this.router,
      getPath: this.getPath
    });
    clone.errorHandler = this.errorHandler;
    clone.#notFoundHandler = this.#notFoundHandler;
    clone.routes = this.routes;
    return clone;
  }
  #notFoundHandler = notFoundHandler;
  // Cannot use `#` because it requires visibility at JavaScript runtime.
  errorHandler = errorHandler;
  /**
   * `.route()` allows grouping other Hono instance in routes.
   *
   * @see {@link https://hono.dev/docs/api/routing#grouping}
   *
   * @param {string} path - base Path
   * @param {Hono} app - other Hono instance
   * @returns {Hono} routed Hono instance
   *
   * @example
   * ```ts
   * const app = new Hono()
   * const app2 = new Hono()
   *
   * app2.get("/user", (c) => c.text("user"))
   * app.route("/api", app2) // GET /api/user
   * ```
   */
  route(path, app2) {
    const subApp = this.basePath(path);
    app2.routes.map((r) => {
      let handler;
      if (app2.errorHandler === errorHandler) {
        handler = r.handler;
      } else {
        handler = /* @__PURE__ */ __name(async (c, next) => (await compose([], app2.errorHandler)(c, () => r.handler(c, next))).res, "handler");
        handler[COMPOSED_HANDLER] = r.handler;
      }
      subApp.#addRoute(r.method, r.path, handler);
    });
    return this;
  }
  /**
   * `.basePath()` allows base paths to be specified.
   *
   * @see {@link https://hono.dev/docs/api/routing#base-path}
   *
   * @param {string} path - base Path
   * @returns {Hono} changed Hono instance
   *
   * @example
   * ```ts
   * const api = new Hono().basePath('/api')
   * ```
   */
  basePath(path) {
    const subApp = this.#clone();
    subApp._basePath = mergePath(this._basePath, path);
    return subApp;
  }
  /**
   * `.onError()` handles an error and returns a customized Response.
   *
   * @see {@link https://hono.dev/docs/api/hono#error-handling}
   *
   * @param {ErrorHandler} handler - request Handler for error
   * @returns {Hono} changed Hono instance
   *
   * @example
   * ```ts
   * app.onError((err, c) => {
   *   console.error(`${err}`)
   *   return c.text('Custom Error Message', 500)
   * })
   * ```
   */
  onError = /* @__PURE__ */ __name((handler) => {
    this.errorHandler = handler;
    return this;
  }, "onError");
  /**
   * `.notFound()` allows you to customize a Not Found Response.
   *
   * @see {@link https://hono.dev/docs/api/hono#not-found}
   *
   * @param {NotFoundHandler} handler - request handler for not-found
   * @returns {Hono} changed Hono instance
   *
   * @example
   * ```ts
   * app.notFound((c) => {
   *   return c.text('Custom 404 Message', 404)
   * })
   * ```
   */
  notFound = /* @__PURE__ */ __name((handler) => {
    this.#notFoundHandler = handler;
    return this;
  }, "notFound");
  /**
   * `.mount()` allows you to mount applications built with other frameworks into your Hono application.
   *
   * @see {@link https://hono.dev/docs/api/hono#mount}
   *
   * @param {string} path - base Path
   * @param {Function} applicationHandler - other Request Handler
   * @param {MountOptions} [options] - options of `.mount()`
   * @returns {Hono} mounted Hono instance
   *
   * @example
   * ```ts
   * import { Router as IttyRouter } from 'itty-router'
   * import { Hono } from 'hono'
   * // Create itty-router application
   * const ittyRouter = IttyRouter()
   * // GET /itty-router/hello
   * ittyRouter.get('/hello', () => new Response('Hello from itty-router'))
   *
   * const app = new Hono()
   * app.mount('/itty-router', ittyRouter.handle)
   * ```
   *
   * @example
   * ```ts
   * const app = new Hono()
   * // Send the request to another application without modification.
   * app.mount('/app', anotherApp, {
   *   replaceRequest: (req) => req,
   * })
   * ```
   */
  mount(path, applicationHandler, options) {
    let replaceRequest;
    let optionHandler;
    if (options) {
      if (typeof options === "function") {
        optionHandler = options;
      } else {
        optionHandler = options.optionHandler;
        if (options.replaceRequest === false) {
          replaceRequest = /* @__PURE__ */ __name((request) => request, "replaceRequest");
        } else {
          replaceRequest = options.replaceRequest;
        }
      }
    }
    const getOptions = optionHandler ? (c) => {
      const options2 = optionHandler(c);
      return Array.isArray(options2) ? options2 : [options2];
    } : (c) => {
      let executionContext = void 0;
      try {
        executionContext = c.executionCtx;
      } catch {
      }
      return [c.env, executionContext];
    };
    replaceRequest ||= (() => {
      const mergedPath = mergePath(this._basePath, path);
      const pathPrefixLength = mergedPath === "/" ? 0 : mergedPath.length;
      return (request) => {
        const url = new URL(request.url);
        url.pathname = url.pathname.slice(pathPrefixLength) || "/";
        return new Request(url, request);
      };
    })();
    const handler = /* @__PURE__ */ __name(async (c, next) => {
      const res = await applicationHandler(replaceRequest(c.req.raw), ...getOptions(c));
      if (res) {
        return res;
      }
      await next();
    }, "handler");
    this.#addRoute(METHOD_NAME_ALL, mergePath(path, "*"), handler);
    return this;
  }
  #addRoute(method, path, handler) {
    method = method.toUpperCase();
    path = mergePath(this._basePath, path);
    const r = { basePath: this._basePath, path, method, handler };
    this.router.add(method, path, [handler, r]);
    this.routes.push(r);
  }
  #handleError(err, c) {
    if (err instanceof Error) {
      return this.errorHandler(err, c);
    }
    throw err;
  }
  #dispatch(request, executionCtx, env2, method) {
    if (method === "HEAD") {
      return (async () => new Response(null, await this.#dispatch(request, executionCtx, env2, "GET")))();
    }
    const path = this.getPath(request, { env: env2 });
    const matchResult = this.router.match(method, path);
    const c = new Context(request, {
      path,
      matchResult,
      env: env2,
      executionCtx,
      notFoundHandler: this.#notFoundHandler
    });
    if (matchResult[0].length === 1) {
      let res;
      try {
        res = matchResult[0][0][0][0](c, async () => {
          c.res = await this.#notFoundHandler(c);
        });
      } catch (err) {
        return this.#handleError(err, c);
      }
      return res instanceof Promise ? res.then(
        (resolved) => resolved || (c.finalized ? c.res : this.#notFoundHandler(c))
      ).catch((err) => this.#handleError(err, c)) : res ?? this.#notFoundHandler(c);
    }
    const composed = compose(matchResult[0], this.errorHandler, this.#notFoundHandler);
    return (async () => {
      try {
        const context = await composed(c);
        if (!context.finalized) {
          throw new Error(
            "Context is not finalized. Did you forget to return a Response object or `await next()`?"
          );
        }
        return context.res;
      } catch (err) {
        return this.#handleError(err, c);
      }
    })();
  }
  /**
   * `.fetch()` will be entry point of your app.
   *
   * @see {@link https://hono.dev/docs/api/hono#fetch}
   *
   * @param {Request} request - request Object of request
   * @param {Env} Env - env Object
   * @param {ExecutionContext} - context of execution
   * @returns {Response | Promise<Response>} response of request
   *
   */
  fetch = /* @__PURE__ */ __name((request, ...rest) => {
    return this.#dispatch(request, rest[1], rest[0], request.method);
  }, "fetch");
  /**
   * `.request()` is a useful method for testing.
   * You can pass a URL or pathname to send a GET request.
   * app will return a Response object.
   * ```ts
   * test('GET /hello is ok', async () => {
   *   const res = await app.request('/hello')
   *   expect(res.status).toBe(200)
   * })
   * ```
   * @see https://hono.dev/docs/api/hono#request
   */
  request = /* @__PURE__ */ __name((input, requestInit, Env, executionCtx) => {
    if (input instanceof Request) {
      return this.fetch(requestInit ? new Request(input, requestInit) : input, Env, executionCtx);
    }
    input = input.toString();
    return this.fetch(
      new Request(
        /^https?:\/\//.test(input) ? input : `http://localhost${mergePath("/", input)}`,
        requestInit
      ),
      Env,
      executionCtx
    );
  }, "request");
  /**
   * `.fire()` automatically adds a global fetch event listener.
   * This can be useful for environments that adhere to the Service Worker API, such as non-ES module Cloudflare Workers.
   * @deprecated
   * Use `fire` from `hono/service-worker` instead.
   * ```ts
   * import { Hono } from 'hono'
   * import { fire } from 'hono/service-worker'
   *
   * const app = new Hono()
   * // ...
   * fire(app)
   * ```
   * @see https://hono.dev/docs/api/hono#fire
   * @see https://developer.mozilla.org/en-US/docs/Web/API/Service_Worker_API
   * @see https://developers.cloudflare.com/workers/reference/migrate-to-module-workers/
   */
  fire = /* @__PURE__ */ __name(() => {
    addEventListener("fetch", (event) => {
      event.respondWith(this.#dispatch(event.request, event, void 0, event.request.method));
    });
  }, "fire");
};

// node_modules/hono/dist/router/reg-exp-router/matcher.js
var emptyParam = [];
function match(method, path) {
  const matchers = this.buildAllMatchers();
  const match2 = /* @__PURE__ */ __name(((method2, path2) => {
    const matcher = matchers[method2] || matchers[METHOD_NAME_ALL];
    const staticMatch = matcher[2][path2];
    if (staticMatch) {
      return staticMatch;
    }
    const match3 = path2.match(matcher[0]);
    if (!match3) {
      return [[], emptyParam];
    }
    const index = match3.indexOf("", 1);
    return [matcher[1][index], match3];
  }), "match2");
  this.match = match2;
  return match2(method, path);
}
__name(match, "match");

// node_modules/hono/dist/router/reg-exp-router/node.js
var LABEL_REG_EXP_STR = "[^/]+";
var ONLY_WILDCARD_REG_EXP_STR = ".*";
var TAIL_WILDCARD_REG_EXP_STR = "(?:|/.*)";
var PATH_ERROR = /* @__PURE__ */ Symbol();
var regExpMetaChars = new Set(".\\+*[^]$()");
function compareKey(a, b) {
  if (a.length === 1) {
    return b.length === 1 ? a < b ? -1 : 1 : -1;
  }
  if (b.length === 1) {
    return 1;
  }
  if (a === ONLY_WILDCARD_REG_EXP_STR || a === TAIL_WILDCARD_REG_EXP_STR) {
    return 1;
  } else if (b === ONLY_WILDCARD_REG_EXP_STR || b === TAIL_WILDCARD_REG_EXP_STR) {
    return -1;
  }
  if (a === LABEL_REG_EXP_STR) {
    return 1;
  } else if (b === LABEL_REG_EXP_STR) {
    return -1;
  }
  return a.length === b.length ? a < b ? -1 : 1 : b.length - a.length;
}
__name(compareKey, "compareKey");
var Node = class _Node {
  static {
    __name(this, "_Node");
  }
  #index;
  #varIndex;
  #children = /* @__PURE__ */ Object.create(null);
  insert(tokens, index, paramMap, context, pathErrorCheckOnly) {
    if (tokens.length === 0) {
      if (this.#index !== void 0) {
        throw PATH_ERROR;
      }
      if (pathErrorCheckOnly) {
        return;
      }
      this.#index = index;
      return;
    }
    const [token, ...restTokens] = tokens;
    const pattern = token === "*" ? restTokens.length === 0 ? ["", "", ONLY_WILDCARD_REG_EXP_STR] : ["", "", LABEL_REG_EXP_STR] : token === "/*" ? ["", "", TAIL_WILDCARD_REG_EXP_STR] : token.match(/^\:([^\{\}]+)(?:\{(.+)\})?$/);
    let node;
    if (pattern) {
      const name = pattern[1];
      let regexpStr = pattern[2] || LABEL_REG_EXP_STR;
      if (name && pattern[2]) {
        if (regexpStr === ".*") {
          throw PATH_ERROR;
        }
        regexpStr = regexpStr.replace(/^\((?!\?:)(?=[^)]+\)$)/, "(?:");
        if (/\((?!\?:)/.test(regexpStr)) {
          throw PATH_ERROR;
        }
      }
      node = this.#children[regexpStr];
      if (!node) {
        if (Object.keys(this.#children).some(
          (k) => k !== ONLY_WILDCARD_REG_EXP_STR && k !== TAIL_WILDCARD_REG_EXP_STR
        )) {
          throw PATH_ERROR;
        }
        if (pathErrorCheckOnly) {
          return;
        }
        node = this.#children[regexpStr] = new _Node();
        if (name !== "") {
          node.#varIndex = context.varIndex++;
        }
      }
      if (!pathErrorCheckOnly && name !== "") {
        paramMap.push([name, node.#varIndex]);
      }
    } else {
      node = this.#children[token];
      if (!node) {
        if (Object.keys(this.#children).some(
          (k) => k.length > 1 && k !== ONLY_WILDCARD_REG_EXP_STR && k !== TAIL_WILDCARD_REG_EXP_STR
        )) {
          throw PATH_ERROR;
        }
        if (pathErrorCheckOnly) {
          return;
        }
        node = this.#children[token] = new _Node();
      }
    }
    node.insert(restTokens, index, paramMap, context, pathErrorCheckOnly);
  }
  buildRegExpStr() {
    const childKeys = Object.keys(this.#children).sort(compareKey);
    const strList = childKeys.map((k) => {
      const c = this.#children[k];
      return (typeof c.#varIndex === "number" ? `(${k})@${c.#varIndex}` : regExpMetaChars.has(k) ? `\\${k}` : k) + c.buildRegExpStr();
    });
    if (typeof this.#index === "number") {
      strList.unshift(`#${this.#index}`);
    }
    if (strList.length === 0) {
      return "";
    }
    if (strList.length === 1) {
      return strList[0];
    }
    return "(?:" + strList.join("|") + ")";
  }
};

// node_modules/hono/dist/router/reg-exp-router/trie.js
var Trie = class {
  static {
    __name(this, "Trie");
  }
  #context = { varIndex: 0 };
  #root = new Node();
  insert(path, index, pathErrorCheckOnly) {
    const paramAssoc = [];
    const groups = [];
    for (let i = 0; ; ) {
      let replaced = false;
      path = path.replace(/\{[^}]+\}/g, (m) => {
        const mark = `@\\${i}`;
        groups[i] = [mark, m];
        i++;
        replaced = true;
        return mark;
      });
      if (!replaced) {
        break;
      }
    }
    const tokens = path.match(/(?::[^\/]+)|(?:\/\*$)|./g) || [];
    for (let i = groups.length - 1; i >= 0; i--) {
      const [mark] = groups[i];
      for (let j = tokens.length - 1; j >= 0; j--) {
        if (tokens[j].indexOf(mark) !== -1) {
          tokens[j] = tokens[j].replace(mark, groups[i][1]);
          break;
        }
      }
    }
    this.#root.insert(tokens, index, paramAssoc, this.#context, pathErrorCheckOnly);
    return paramAssoc;
  }
  buildRegExp() {
    let regexp = this.#root.buildRegExpStr();
    if (regexp === "") {
      return [/^$/, [], []];
    }
    let captureIndex = 0;
    const indexReplacementMap = [];
    const paramReplacementMap = [];
    regexp = regexp.replace(/#(\d+)|@(\d+)|\.\*\$/g, (_, handlerIndex, paramIndex) => {
      if (handlerIndex !== void 0) {
        indexReplacementMap[++captureIndex] = Number(handlerIndex);
        return "$()";
      }
      if (paramIndex !== void 0) {
        paramReplacementMap[Number(paramIndex)] = ++captureIndex;
        return "";
      }
      return "";
    });
    return [new RegExp(`^${regexp}`), indexReplacementMap, paramReplacementMap];
  }
};

// node_modules/hono/dist/router/reg-exp-router/router.js
var nullMatcher = [/^$/, [], /* @__PURE__ */ Object.create(null)];
var wildcardRegExpCache = /* @__PURE__ */ Object.create(null);
function buildWildcardRegExp(path) {
  return wildcardRegExpCache[path] ??= new RegExp(
    path === "*" ? "" : `^${path.replace(
      /\/\*$|([.\\+*[^\]$()])/g,
      (_, metaChar) => metaChar ? `\\${metaChar}` : "(?:|/.*)"
    )}$`
  );
}
__name(buildWildcardRegExp, "buildWildcardRegExp");
function clearWildcardRegExpCache() {
  wildcardRegExpCache = /* @__PURE__ */ Object.create(null);
}
__name(clearWildcardRegExpCache, "clearWildcardRegExpCache");
function buildMatcherFromPreprocessedRoutes(routes) {
  const trie = new Trie();
  const handlerData = [];
  if (routes.length === 0) {
    return nullMatcher;
  }
  const routesWithStaticPathFlag = routes.map(
    (route) => [!/\*|\/:/.test(route[0]), ...route]
  ).sort(
    ([isStaticA, pathA], [isStaticB, pathB]) => isStaticA ? 1 : isStaticB ? -1 : pathA.length - pathB.length
  );
  const staticMap = /* @__PURE__ */ Object.create(null);
  for (let i = 0, j = -1, len = routesWithStaticPathFlag.length; i < len; i++) {
    const [pathErrorCheckOnly, path, handlers] = routesWithStaticPathFlag[i];
    if (pathErrorCheckOnly) {
      staticMap[path] = [handlers.map(([h]) => [h, /* @__PURE__ */ Object.create(null)]), emptyParam];
    } else {
      j++;
    }
    let paramAssoc;
    try {
      paramAssoc = trie.insert(path, j, pathErrorCheckOnly);
    } catch (e) {
      throw e === PATH_ERROR ? new UnsupportedPathError(path) : e;
    }
    if (pathErrorCheckOnly) {
      continue;
    }
    handlerData[j] = handlers.map(([h, paramCount]) => {
      const paramIndexMap = /* @__PURE__ */ Object.create(null);
      paramCount -= 1;
      for (; paramCount >= 0; paramCount--) {
        const [key, value] = paramAssoc[paramCount];
        paramIndexMap[key] = value;
      }
      return [h, paramIndexMap];
    });
  }
  const [regexp, indexReplacementMap, paramReplacementMap] = trie.buildRegExp();
  for (let i = 0, len = handlerData.length; i < len; i++) {
    for (let j = 0, len2 = handlerData[i].length; j < len2; j++) {
      const map = handlerData[i][j]?.[1];
      if (!map) {
        continue;
      }
      const keys = Object.keys(map);
      for (let k = 0, len3 = keys.length; k < len3; k++) {
        map[keys[k]] = paramReplacementMap[map[keys[k]]];
      }
    }
  }
  const handlerMap = [];
  for (const i in indexReplacementMap) {
    handlerMap[i] = handlerData[indexReplacementMap[i]];
  }
  return [regexp, handlerMap, staticMap];
}
__name(buildMatcherFromPreprocessedRoutes, "buildMatcherFromPreprocessedRoutes");
function findMiddleware(middleware, path) {
  if (!middleware) {
    return void 0;
  }
  for (const k of Object.keys(middleware).sort((a, b) => b.length - a.length)) {
    if (buildWildcardRegExp(k).test(path)) {
      return [...middleware[k]];
    }
  }
  return void 0;
}
__name(findMiddleware, "findMiddleware");
var RegExpRouter = class {
  static {
    __name(this, "RegExpRouter");
  }
  name = "RegExpRouter";
  #middleware;
  #routes;
  constructor() {
    this.#middleware = { [METHOD_NAME_ALL]: /* @__PURE__ */ Object.create(null) };
    this.#routes = { [METHOD_NAME_ALL]: /* @__PURE__ */ Object.create(null) };
  }
  add(method, path, handler) {
    const middleware = this.#middleware;
    const routes = this.#routes;
    if (!middleware || !routes) {
      throw new Error(MESSAGE_MATCHER_IS_ALREADY_BUILT);
    }
    if (!middleware[method]) {
      ;
      [middleware, routes].forEach((handlerMap) => {
        handlerMap[method] = /* @__PURE__ */ Object.create(null);
        Object.keys(handlerMap[METHOD_NAME_ALL]).forEach((p) => {
          handlerMap[method][p] = [...handlerMap[METHOD_NAME_ALL][p]];
        });
      });
    }
    if (path === "/*") {
      path = "*";
    }
    const paramCount = (path.match(/\/:/g) || []).length;
    if (/\*$/.test(path)) {
      const re = buildWildcardRegExp(path);
      if (method === METHOD_NAME_ALL) {
        Object.keys(middleware).forEach((m) => {
          middleware[m][path] ||= findMiddleware(middleware[m], path) || findMiddleware(middleware[METHOD_NAME_ALL], path) || [];
        });
      } else {
        middleware[method][path] ||= findMiddleware(middleware[method], path) || findMiddleware(middleware[METHOD_NAME_ALL], path) || [];
      }
      Object.keys(middleware).forEach((m) => {
        if (method === METHOD_NAME_ALL || method === m) {
          Object.keys(middleware[m]).forEach((p) => {
            re.test(p) && middleware[m][p].push([handler, paramCount]);
          });
        }
      });
      Object.keys(routes).forEach((m) => {
        if (method === METHOD_NAME_ALL || method === m) {
          Object.keys(routes[m]).forEach(
            (p) => re.test(p) && routes[m][p].push([handler, paramCount])
          );
        }
      });
      return;
    }
    const paths = checkOptionalParameter(path) || [path];
    for (let i = 0, len = paths.length; i < len; i++) {
      const path2 = paths[i];
      Object.keys(routes).forEach((m) => {
        if (method === METHOD_NAME_ALL || method === m) {
          routes[m][path2] ||= [
            ...findMiddleware(middleware[m], path2) || findMiddleware(middleware[METHOD_NAME_ALL], path2) || []
          ];
          routes[m][path2].push([handler, paramCount - len + i + 1]);
        }
      });
    }
  }
  match = match;
  buildAllMatchers() {
    const matchers = /* @__PURE__ */ Object.create(null);
    Object.keys(this.#routes).concat(Object.keys(this.#middleware)).forEach((method) => {
      matchers[method] ||= this.#buildMatcher(method);
    });
    this.#middleware = this.#routes = void 0;
    clearWildcardRegExpCache();
    return matchers;
  }
  #buildMatcher(method) {
    const routes = [];
    let hasOwnRoute = method === METHOD_NAME_ALL;
    [this.#middleware, this.#routes].forEach((r) => {
      const ownRoute = r[method] ? Object.keys(r[method]).map((path) => [path, r[method][path]]) : [];
      if (ownRoute.length !== 0) {
        hasOwnRoute ||= true;
        routes.push(...ownRoute);
      } else if (method !== METHOD_NAME_ALL) {
        routes.push(
          ...Object.keys(r[METHOD_NAME_ALL]).map((path) => [path, r[METHOD_NAME_ALL][path]])
        );
      }
    });
    if (!hasOwnRoute) {
      return null;
    } else {
      return buildMatcherFromPreprocessedRoutes(routes);
    }
  }
};

// node_modules/hono/dist/router/smart-router/router.js
var SmartRouter = class {
  static {
    __name(this, "SmartRouter");
  }
  name = "SmartRouter";
  #routers = [];
  #routes = [];
  constructor(init) {
    this.#routers = init.routers;
  }
  add(method, path, handler) {
    if (!this.#routes) {
      throw new Error(MESSAGE_MATCHER_IS_ALREADY_BUILT);
    }
    this.#routes.push([method, path, handler]);
  }
  match(method, path) {
    if (!this.#routes) {
      throw new Error("Fatal error");
    }
    const routers = this.#routers;
    const routes = this.#routes;
    const len = routers.length;
    let i = 0;
    let res;
    for (; i < len; i++) {
      const router = routers[i];
      try {
        for (let i2 = 0, len2 = routes.length; i2 < len2; i2++) {
          router.add(...routes[i2]);
        }
        res = router.match(method, path);
      } catch (e) {
        if (e instanceof UnsupportedPathError) {
          continue;
        }
        throw e;
      }
      this.match = router.match.bind(router);
      this.#routers = [router];
      this.#routes = void 0;
      break;
    }
    if (i === len) {
      throw new Error("Fatal error");
    }
    this.name = `SmartRouter + ${this.activeRouter.name}`;
    return res;
  }
  get activeRouter() {
    if (this.#routes || this.#routers.length !== 1) {
      throw new Error("No active router has been determined yet.");
    }
    return this.#routers[0];
  }
};

// node_modules/hono/dist/router/trie-router/node.js
var emptyParams = /* @__PURE__ */ Object.create(null);
var hasChildren = /* @__PURE__ */ __name((children) => {
  for (const _ in children) {
    return true;
  }
  return false;
}, "hasChildren");
var Node2 = class _Node2 {
  static {
    __name(this, "_Node");
  }
  #methods;
  #children;
  #patterns;
  #order = 0;
  #params = emptyParams;
  constructor(method, handler, children) {
    this.#children = children || /* @__PURE__ */ Object.create(null);
    this.#methods = [];
    if (method && handler) {
      const m = /* @__PURE__ */ Object.create(null);
      m[method] = { handler, possibleKeys: [], score: 0 };
      this.#methods = [m];
    }
    this.#patterns = [];
  }
  insert(method, path, handler) {
    this.#order = ++this.#order;
    let curNode = this;
    const parts = splitRoutingPath(path);
    const possibleKeys = [];
    for (let i = 0, len = parts.length; i < len; i++) {
      const p = parts[i];
      const nextP = parts[i + 1];
      const pattern = getPattern(p, nextP);
      const key = Array.isArray(pattern) ? pattern[0] : p;
      if (key in curNode.#children) {
        curNode = curNode.#children[key];
        if (pattern) {
          possibleKeys.push(pattern[1]);
        }
        continue;
      }
      curNode.#children[key] = new _Node2();
      if (pattern) {
        curNode.#patterns.push(pattern);
        possibleKeys.push(pattern[1]);
      }
      curNode = curNode.#children[key];
    }
    curNode.#methods.push({
      [method]: {
        handler,
        possibleKeys: possibleKeys.filter((v, i, a) => a.indexOf(v) === i),
        score: this.#order
      }
    });
    return curNode;
  }
  #pushHandlerSets(handlerSets, node, method, nodeParams, params) {
    for (let i = 0, len = node.#methods.length; i < len; i++) {
      const m = node.#methods[i];
      const handlerSet = m[method] || m[METHOD_NAME_ALL];
      const processedSet = {};
      if (handlerSet !== void 0) {
        handlerSet.params = /* @__PURE__ */ Object.create(null);
        handlerSets.push(handlerSet);
        if (nodeParams !== emptyParams || params && params !== emptyParams) {
          for (let i2 = 0, len2 = handlerSet.possibleKeys.length; i2 < len2; i2++) {
            const key = handlerSet.possibleKeys[i2];
            const processed = processedSet[handlerSet.score];
            handlerSet.params[key] = params?.[key] && !processed ? params[key] : nodeParams[key] ?? params?.[key];
            processedSet[handlerSet.score] = true;
          }
        }
      }
    }
  }
  search(method, path) {
    const handlerSets = [];
    this.#params = emptyParams;
    const curNode = this;
    let curNodes = [curNode];
    const parts = splitPath(path);
    const curNodesQueue = [];
    const len = parts.length;
    let partOffsets = null;
    for (let i = 0; i < len; i++) {
      const part = parts[i];
      const isLast = i === len - 1;
      const tempNodes = [];
      for (let j = 0, len2 = curNodes.length; j < len2; j++) {
        const node = curNodes[j];
        const nextNode = node.#children[part];
        if (nextNode) {
          nextNode.#params = node.#params;
          if (isLast) {
            if (nextNode.#children["*"]) {
              this.#pushHandlerSets(handlerSets, nextNode.#children["*"], method, node.#params);
            }
            this.#pushHandlerSets(handlerSets, nextNode, method, node.#params);
          } else {
            tempNodes.push(nextNode);
          }
        }
        for (let k = 0, len3 = node.#patterns.length; k < len3; k++) {
          const pattern = node.#patterns[k];
          const params = node.#params === emptyParams ? {} : { ...node.#params };
          if (pattern === "*") {
            const astNode = node.#children["*"];
            if (astNode) {
              this.#pushHandlerSets(handlerSets, astNode, method, node.#params);
              astNode.#params = params;
              tempNodes.push(astNode);
            }
            continue;
          }
          const [key, name, matcher] = pattern;
          if (!part && !(matcher instanceof RegExp)) {
            continue;
          }
          const child = node.#children[key];
          if (matcher instanceof RegExp) {
            if (partOffsets === null) {
              partOffsets = new Array(len);
              let offset = path[0] === "/" ? 1 : 0;
              for (let p = 0; p < len; p++) {
                partOffsets[p] = offset;
                offset += parts[p].length + 1;
              }
            }
            const restPathString = path.substring(partOffsets[i]);
            const m = matcher.exec(restPathString);
            if (m) {
              params[name] = m[0];
              this.#pushHandlerSets(handlerSets, child, method, node.#params, params);
              if (hasChildren(child.#children)) {
                child.#params = params;
                const componentCount = m[0].match(/\//)?.length ?? 0;
                const targetCurNodes = curNodesQueue[componentCount] ||= [];
                targetCurNodes.push(child);
              }
              continue;
            }
          }
          if (matcher === true || matcher.test(part)) {
            params[name] = part;
            if (isLast) {
              this.#pushHandlerSets(handlerSets, child, method, params, node.#params);
              if (child.#children["*"]) {
                this.#pushHandlerSets(
                  handlerSets,
                  child.#children["*"],
                  method,
                  params,
                  node.#params
                );
              }
            } else {
              child.#params = params;
              tempNodes.push(child);
            }
          }
        }
      }
      const shifted = curNodesQueue.shift();
      curNodes = shifted ? tempNodes.concat(shifted) : tempNodes;
    }
    if (handlerSets.length > 1) {
      handlerSets.sort((a, b) => {
        return a.score - b.score;
      });
    }
    return [handlerSets.map(({ handler, params }) => [handler, params])];
  }
};

// node_modules/hono/dist/router/trie-router/router.js
var TrieRouter = class {
  static {
    __name(this, "TrieRouter");
  }
  name = "TrieRouter";
  #node;
  constructor() {
    this.#node = new Node2();
  }
  add(method, path, handler) {
    const results = checkOptionalParameter(path);
    if (results) {
      for (let i = 0, len = results.length; i < len; i++) {
        this.#node.insert(method, results[i], handler);
      }
      return;
    }
    this.#node.insert(method, path, handler);
  }
  match(method, path) {
    return this.#node.search(method, path);
  }
};

// node_modules/hono/dist/hono.js
var Hono2 = class extends Hono {
  static {
    __name(this, "Hono");
  }
  /**
   * Creates an instance of the Hono class.
   *
   * @param options - Optional configuration options for the Hono instance.
   */
  constructor(options = {}) {
    super(options);
    this.router = options.router ?? new SmartRouter({
      routers: [new RegExpRouter(), new TrieRouter()]
    });
  }
};

// src/proxy.ts
var MAX_ATTEMPTS = 2;
var RETRY_DELAY_MS = 1500;
var REQUEST_TIMEOUT_MS = 45e3;
async function proxyToOrigin(request, originUrl) {
  const start = Date.now();
  const incomingUrl = new URL(request.url);
  const targetUrl = originUrl.replace(/\/$/, "") + incomingUrl.pathname + incomingUrl.search;
  const fwdHeaders = new Headers(request.headers);
  fwdHeaders.delete("host");
  fwdHeaders.delete("cf-connecting-ip");
  fwdHeaders.delete("cf-ipcountry");
  fwdHeaders.delete("cf-ray");
  fwdHeaders.delete("cf-visitor");
  fwdHeaders.set("x-forwarded-for", request.headers.get("cf-connecting-ip") ?? "");
  fwdHeaders.set("x-forwarded-host", incomingUrl.host);
  fwdHeaders.set("x-forwarded-proto", "https");
  fwdHeaders.set("x-edge-source", "cloudflare-workers");
  let bodyBytes = null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    bodyBytes = await request.arrayBuffer();
  }
  let lastError = null;
  let attempts = 0;
  let retried = false;
  for (let i = 0; i < MAX_ATTEMPTS; i++) {
    attempts++;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(targetUrl, {
        method: request.method,
        headers: fwdHeaders,
        body: bodyBytes ?? void 0,
        signal: controller.signal
        // Workers does NOT need redirect:'manual' — default is fine
      });
      clearTimeout(timer);
      const isColdStart = response.status === 502 || response.status === 503 || response.status === 504;
      if (isColdStart && i < MAX_ATTEMPTS - 1) {
        retried = true;
        await response.body?.cancel();
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
        continue;
      }
      return { response, attempts, totalMs: Date.now() - start, retried };
    } catch (err) {
      clearTimeout(timer);
      lastError = err;
      if (i < MAX_ATTEMPTS - 1) {
        retried = true;
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
        continue;
      }
    }
  }
  const errBody = JSON.stringify({
    error: "origin_unreachable",
    message: "Upstream service did not respond. Edge retried " + attempts + " time(s).",
    detail: String(lastError ?? "see status code"),
    edge: "cloudflare-workers"
  });
  return {
    response: new Response(errBody, {
      status: 503,
      headers: {
        "content-type": "application/json",
        "retry-after": "30"
      }
    }),
    attempts,
    totalMs: Date.now() - start,
    retried: true
  };
}
__name(proxyToOrigin, "proxyToOrigin");

// src/snapshots/manifest.json
var manifest_default = { schema_version: "1.0", service: { name: "SMB Transaction & Communication Broker", id: "smb-broker", version: "0.1.0", description: "A horizontal, agent-callable broker that lets an autonomous agent discover, verify, communicate with, schedule with, and transact with the long tail of small and mid-size businesses through one clean tool surface. Routes internally through whatever channel actually reaches the SMB: direct API, voice AI, SMS, email, web form, or browser automation.", base_url: "https://api.smb-broker.example/v1", discovery_url: "https://api.smb-broker.example/.well-known/agent-manifest.json", contact: "support@smb-broker.example" }, operations: [{ name: "find_business", description: "Given criteria (vertical, location, capability, price band, availability window), return ranked candidate SMBs from the verified supply network. Returns only curated, verified, transactable businesses \u2014 not raw directory results.", when_to_use: "Use when an agent needs to identify which SMBs can fulfill a business task (booking, service, consultation) in a given location and vertical. Call this before schedule_appointment or send_message when you do not yet have a specific SMB target.", when_not_to_use: "Do not use as a general directory or browsing surface. Do not use when you already have a specific verified SMB identifier. Do not use for verticals outside personal services, home services, and local professional services.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["vertical", "location"], properties: { vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services"], description: "Service vertical to search within" }, location: { type: "object", required: ["zip_or_city"], properties: { zip_or_city: { type: "string" }, radius_miles: { type: "number", default: 10 } } }, capability: { type: "string", description: "Specific service capability required, e.g. 'haircut', 'plumbing', 'tax_consultation'" }, price_band: { type: "object", properties: { max_usd: { type: "number" } } }, availability_window: { type: "object", properties: { start_iso: { type: "string", format: "date-time" }, end_iso: { type: "string", format: "date-time" } } }, max_results: { type: "integer", default: 5, maximum: 20 } } }, output_schema: { type: "object", properties: { businesses: { type: "array", items: { type: "object", properties: { smb_id: { type: "string" }, name: { type: "string" }, vertical: { type: "string" }, address: { type: "string" }, capabilities: { type: "array", items: { type: "string" } }, channels_available: { type: "array", items: { type: "string" } }, price_range: { type: "object" }, verified_at: { type: "string", format: "date-time" }, rank_score: { type: "number" } } } }, total_in_supply_network: { type: "integer" }, supply_coverage_note: { type: "string" } } }, cost_model: { basis: "per_call", unit_price_usd: 0.01, tiers: [{ calls_per_month: 0, price_usd: 0.01 }, { calls_per_month: 1e4, price_usd: 5e-3 }] }, slo: { p50_ms: 200, p95_ms: 800, success_rate_30d: null, availability_30d: null, supply_network_coverage_by_vertical: null }, idempotency: "read-only \u2014 idempotency key not required", failure_modes: ["bad_input", "missing_capability", "rate_limited", "out_of_supply_network", "transient", "internal"], examples: [{ label: "Happy path \u2014 find hair salon near Atlanta", input: { vertical: "personal_services", location: { zip_or_city: "30309" }, capability: "haircut", price_band: { max_usd: 50 }, availability_window: { start_iso: "2026-04-29T09:00:00Z", end_iso: "2026-04-29T12:00:00Z" } }, output: { status: "success", businesses: [{ smb_id: "smb_001", name: "Cuts & Co.", vertical: "personal_services", address: "123 Main St, Atlanta, GA 30309", capabilities: ["haircut", "blowdry"], channels_available: ["direct_api:square", "sms"], price_range: { min_usd: 35, max_usd: 55 }, verified_at: "2026-04-20T00:00:00Z", rank_score: 0.92 }] } }, { label: "No results in supply network", input: { vertical: "professional_services", location: { zip_or_city: "99999" }, capability: "tax_consultation" }, output: { status: "success", businesses: [], total_in_supply_network: 0, supply_coverage_note: "No verified businesses in this area for this capability. Consider expanding radius_miles." } }, { label: "Channel fallback note in result", input: { vertical: "home_services", location: { zip_or_city: "02139" }, capability: "plumbing" }, output: { status: "success", businesses: [{ smb_id: "smb_044", name: "FastFix Plumbing", channels_available: ["voice_ai", "sms"], capabilities: ["plumbing", "emergency_plumbing"] }], supply_coverage_note: "This business is reachable via voice AI or SMS only \u2014 no direct scheduling API." } }], user_query_examples: [{ user_says: "Find me a salon in Tokyo that does color", agent_call: { tool: "find_business", arguments: { vertical: "personal_services", location: { zip_or_city: "Tokyo" }, capability: "color" } } }, { user_says: "I need a plumber near 30309", agent_call: { tool: "find_business", arguments: { vertical: "home_services", location: { zip_or_city: "30309" }, capability: "plumbing" } } }, { user_says: "Show me dentists in London", agent_call: { tool: "find_business", arguments: { vertical: "professional_services", location: { zip_or_city: "London" }, capability: "dentist" } } }] }, { name: "verify_business", description: "Confirm that an SMB is real, currently operating, and capable of the requested service. Performs a live capability probe against the business's channel.", when_to_use: "Use before sending communications or scheduling if you have an unverified SMB identifier, or if the agent's task requires confirmed capability (e.g., 'I need to be sure they do emergency plumbing').", when_not_to_use: "Do not use if the SMB was returned from find_business within the last 24 hours \u2014 those results are already verified.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["smb_id"], properties: { smb_id: { type: "string" }, capability_to_verify: { type: "string" } } }, output_schema: { type: "object", properties: { verified: { type: "boolean" }, capabilities_confirmed: { type: "array", items: { type: "string" } }, channels_reachable: { type: "array", items: { type: "string" } }, last_verified_at: { type: "string", format: "date-time" }, verification_method: { type: "string" } } }, cost_model: { basis: "per_call", unit_price_usd: 0.02 }, slo: { p50_ms: 500, p95_ms: 2e3 }, idempotency: "read-only \u2014 idempotency key not required", failure_modes: ["bad_input", "supply_unreachable", "supply_unverified", "transient"], examples: [{ label: "Verified with direct API", input: { smb_id: "smb_001", capability_to_verify: "haircut" }, output: { status: "success", result: { verified: true, capabilities_confirmed: ["haircut", "blowdry"], channels_reachable: ["direct_api:square"], verification_method: "direct_api_probe" } } }], user_query_examples: [{ user_says: "Confirm smb_imp_abc actually does emergency plumbing", agent_call: { tool: "verify_business", arguments: { smb_id: "smb_imp_abc", capability_to_verify: "emergency_plumbing" } } }] }, { name: "send_message", description: "Send a message on behalf of an agent's user or an SMB across SMS, email, or voice. Five message types: transactional, reminder, follow_up, notification, marketing. Every send routes through a non-bypassable compliance gate (TCPA, GDPR, CASL, PDPL across 22 jurisdictions) that enforces opt-in consent for marketing/promotional content \u2014 marketing without recorded consent is rejected at runtime with a structured compliance_violation receipt. Channel is abstracted: specify intent and recipient; the service selects and falls back across channels.", when_to_use: "Use to: (a) confirm a booking the agent just made, (b) reply to a customer who messaged the SMB first, (c) follow up on a quote the user requested, (d) send appointment reminders the SMB owes its customer, (e) send marketing messages to recipients who have opted in (with consent_record_id). The gate verifies consent on every send.", when_not_to_use: "Do NOT use for OTPs or critical transactional confirmations \u2014 use send_transactional_confirmation. Do NOT attempt to send marketing without a consent_record_id pointing at a real opt-in \u2014 the gate will reject the send and log a compliance_violation. Do NOT attempt bulk / list-based / drip / cold outreach \u2014 those are out of scope and the rate limiter will throttle abuse.", execution_profile: "sync_fast", compliance_constraints: ["Permitted message types: transactional, marketing, reminder, follow_up, notification.", "Marketing messages require a valid consent_record_id at send time \u2014 the gate looks it up in the consent_store and rejects if missing, expired, or revoked.", "US SMS marketing requires TCPA prior express written consent + 10DLC campaign registration.", "EU/UK recipients require explicit GDPR lawful basis (contract or freely-given consent) for marketing messages.", "Canadian recipients require CASL express consent for commercial electronic messages.", "Voice channel to US recipients requires prior express consent for autodialed/prerecorded calls (TCPA).", "All commercial email contains a functional unsubscribe link (CAN-SPAM).", "GCC recipients (UAE, SA, OM, QA, KW, BH) covered by PDPL-style rules per jurisdiction; the gate routes by country_code."], input_schema: { type: "object", required: ["recipient", "message_type", "content"], properties: { recipient: { type: "object", required: ["id_type", "id_value"], properties: { id_type: { type: "string", enum: ["phone", "email", "smb_id", "customer_id"] }, id_value: { type: "string" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2, required for compliance routing" } } }, message_type: { type: "string", description: "Intent tag for the message. Five permitted types. 'marketing' is allowed only when paired with a valid consent_record_id; the compliance gate verifies the consent at send time and rejects (compliance_violation receipt) if it's missing, expired, or revoked.", enum: ["transactional", "marketing", "reminder", "follow_up", "notification"] }, content: { type: "object", required: ["body"], properties: { body: { type: "string" }, subject: { type: "string", description: "For email channel" }, template_id: { type: "string" }, template_vars: { type: "object" } } }, preferred_channel: { type: "string", enum: ["sms", "email", "voice", "auto"], default: "auto" }, send_at_iso: { type: "string", format: "date-time", description: "Schedule for future delivery; omit for immediate" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_message", unit_price_usd: 0.02, voice_premium_usd: 0.2 }, slo: { p50_ms: 800, p95_ms: 4e3 }, idempotency: "required \u2014 use Idempotency-Key header to prevent duplicate sends", failure_modes: ["bad_input", "compliance_violation", "consent_missing", "supply_unreachable", "upstream_failure", "rate_limited", "budget_exceeded"], examples: [{ label: "SMS appointment reminder \u2014 happy path", input: { recipient: { id_type: "phone", id_value: "+14045550100", country_code: "US" }, message_type: "reminder", content: { body: "Reminder: your appointment at Cuts & Co. is tomorrow at 10am. Reply STOP to unsubscribe." }, preferred_channel: "sms" }, output: { status: "success", channel_used: "sms:twilio", cost: { amount: 0.05, currency: "USD" } } }, { label: "Consumer asked agent to follow up on a quote", input: { recipient: { id_type: "smb_id", id_value: "smb_044", country_code: "US" }, message_type: "follow_up", content: { body: "Hi \u2014 checking back on the plumbing quote I requested yesterday for Maple St. Could you share availability for Thursday?" } }, output: { status: "success", channel_used: "sms:twilio", cost: { amount: 0.02, currency: "USD" } } }, { label: "Marketing SMS blocked \u2014 no recorded consent", input: { recipient: { id_type: "phone", id_value: "+14045550200", country_code: "US" }, message_type: "marketing", content: { body: "20% off this week only!" } }, output: { status: "failure", reason_code: "compliance_violation", human_message: "Recipient +14045550200 has not opted in to marketing SMS. TCPA prior express written consent is required. Obtain consent (consent_record_id) and retry.", retriable: false } }, { label: "Marketing SMS to an opted-in subscriber (consent passes the gate)", input: { recipient: { id_type: "phone", id_value: "+14045550100", country_code: "US" }, message_type: "marketing", content: { body: "Cuts & Co.: Fall sale this Saturday \u2014 20% off cuts + color. Reply STOP to opt out." } }, output: { status: "success", channel_used: "sms:twilio", cost: { amount: 0.02, currency: "USD" } } }, { label: "Voice fallback when SMS unreachable", input: { recipient: { id_type: "phone", id_value: "+12125550300", country_code: "US" }, message_type: "reminder", content: { body: "Your appointment is tomorrow at 2pm." } }, output: { status: "success", channel_used: "voice_ai:vapi", channel_fallback_chain: ["sms:twilio (carrier_filter)", "voice_ai:vapi (success)"], cost: { amount: 0.3, currency: "USD" } } }], user_query_examples: [{ user_says: "Text the salon I'll be 10 minutes late", agent_call: { tool: "send_message", arguments: { recipient_id: "smb_xyz", channel_preference: "sms", message: { body: "Will be 10 minutes late." }, country_code: "US" } } }, { user_says: "Email the dentist about insurance", agent_call: { tool: "send_message", arguments: { recipient_id: "smb_xyz", channel_preference: "email", message: { body: "Do you accept Cigna?" } } } }] }, { name: "capture_lead", description: "Structured intake of a prospect into an SMB's funnel with validation, enrichment hooks, and deduplication. Inserts into the SMB's CRM or direct-booking pipeline if available.", when_to_use: "Use when a potential customer has expressed interest in an SMB's service and you want to ensure they are registered in the SMB's pipeline for follow-up.", when_not_to_use: "Do not use for confirmed bookings \u2014 use schedule_appointment. Do not use for bulk list imports.", execution_profile: "sync_fast", compliance_constraints: ["Captured lead data is subject to GDPR data subject rights for EU residents.", "CAN-SPAM applies if captured email will receive commercial messages.", "Retention policy is enforced per jurisdiction by ComplianceAgent."], input_schema: { type: "object", required: ["smb_id", "prospect"], properties: { smb_id: { type: "string" }, prospect: { type: "object", required: ["name"], properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string", format: "email" }, service_interest: { type: "string" }, notes: { type: "string" }, consent_record_id: { type: "string", description: "Optional ID of a consent record proving the prospect asked to be contacted (e.g., they filled an SMB's intake form or requested a quote). Required when downstream send_message calls are anticipated." } } }, source: { type: "string", description: "Where the consumer-initiated request originated (e.g., 'consumer_request', 'inbound_quote_form', 'agent_referral_from_find_business')." } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_lead", unit_price_usd: 0.05 }, slo: { p50_ms: 600, p95_ms: 3e3 }, idempotency: "required \u2014 dedupe is keyed on (smb_id, prospect.phone or prospect.email)", failure_modes: ["bad_input", "idempotency_conflict", "upstream_failure", "compliance_violation"], examples: [{ label: "Happy path \u2014 lead captured into Square CRM", input: { smb_id: "smb_001", prospect: { name: "Jane Smith", phone: "+14045551234", email: "jane@example.com", service_interest: "haircut" }, source: "agent_referral" }, output: { status: "success", result: { lead_id: "lead_abc123", channel_used: "direct_api:square" } } }], user_query_examples: [{ user_says: "Tell smb_xyz I'm interested and want a callback", agent_call: { tool: "capture_lead", arguments: { smb_id: "smb_xyz", prospect: { name: "Jane", phone: "+15551234567", email: "jane@example.com" }, source: "agent" } } }] }, { name: "schedule_appointment", description: "Availability lookup, hold, confirm, reschedule, or cancel appointments with an SMB. Routes through the SMB's native booking system if available, falls back to voice AI or web form.", when_to_use: "Use when an agent needs to book, reschedule, or cancel a specific appointment with a specific SMB. Requires a verified smb_id.", when_not_to_use: "Do not use for bulk scheduling. Do not use without a verified SMB \u2014 call find_business and verify_business first if needed.", execution_profile: "async_by_default", compliance_constraints: ["Voice channel to US recipients requires prior express consent for prerecorded calls (TCPA).", "Voice recording in CA, FL, IL, MD, MA, MT, NV, NH, PA, WA requires two-party consent \u2014 recording-consent prompt fires automatically."], input_schema: { type: "object", required: ["smb_id", "action"], properties: { smb_id: { type: "string" }, action: { type: "string", enum: ["book", "reschedule", "cancel", "check_availability"] }, service: { type: "string" }, customer: { type: "object", properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string" } } }, requested_time: { type: "object", properties: { preferred_iso: { type: "string", format: "date-time" }, window_start_iso: { type: "string", format: "date-time" }, window_end_iso: { type: "string", format: "date-time" }, duration_minutes: { type: "integer" } } }, existing_appointment_id: { type: "string", description: "Required for reschedule/cancel" }, notes: { type: "string" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_booking_attempt", unit_price_usd: 0.15, success_bonus_usd: 0.35 }, slo: { p50_ms: 5e3, p95_ms: 6e4, note: "async \u2014 p50/p95 reflect time to terminal outcome, not HTTP response" }, idempotency: "required \u2014 prevents double-booking on network retry", failure_modes: ["bad_input", "supply_unreachable", "upstream_failure", "compliance_violation", "recording_consent_missing", "outcome_rejected"], examples: [{ label: "Book haircut \u2014 direct API path", input: { smb_id: "smb_001", action: "book", service: "haircut", customer: { name: "Alex Johnson", phone: "+14045559999" }, requested_time: { window_start_iso: "2026-04-29T09:00:00Z", window_end_iso: "2026-04-29T12:00:00Z" } }, output: { status: "pending_async", operation_id: "op_xyz789", estimated_completion_time: "2026-04-27T10:01:00Z", next_actions: ["poll get_status with operation_id op_xyz789", "or await webhook callback"] } }, { label: "Book plumber \u2014 voice AI fallback", input: { smb_id: "smb_044", action: "book", service: "emergency_plumbing", requested_time: { preferred_iso: "2026-04-28T08:00:00Z" } }, output: { status: "pending_async", channel_fallback_chain: ["direct_api:none", "voice_ai:vapi (dispatched)"], operation_id: "op_abc001" } }, { label: "Cancel appointment", input: { smb_id: "smb_001", action: "cancel", existing_appointment_id: "appt_cal_4455" }, output: { status: "success", reason_code: "cancelled", result: { refund_status: "pending", confirmation_number: "CANCEL-789" } } }], user_query_examples: [{ user_says: "Book the haircut for next Tuesday at 3pm", agent_call: { tool: "schedule_appointment", arguments: { smb_id: "smb_imp_abc", action: "book", service: "haircut" } } }, { user_says: "Cancel my Friday appointment at smb_xyz", agent_call: { tool: "schedule_appointment", arguments: { smb_id: "smb_xyz", action: "cancel" } } }, { user_says: "Reschedule my dental cleaning to next week", agent_call: { tool: "schedule_appointment", arguments: { smb_id: "smb_imp_xyz", action: "reschedule" } } }] }, { name: "send_transactional_confirmation", description: "Idempotent transactional messages: OTPs, booking confirmations, payment receipts, cancellation notices. Guaranteed delivery via redundant channels.", when_to_use: "Use for any message that MUST be delivered reliably \u2014 OTPs, booking confirmations, receipts. Do not use for marketing.", when_not_to_use: "Do not use for marketing or promotional messages. Do not use for conversational messages.", execution_profile: "sync_fast", compliance_constraints: ["Transactional messages are exempt from marketing consent rules but must include clear identification of sender and purpose."], input_schema: { type: "object", required: ["recipient", "confirmation_type", "data"], properties: { recipient: { type: "object", required: ["phone_or_email"], properties: { phone_or_email: { type: "string" }, name: { type: "string" } } }, confirmation_type: { type: "string", enum: ["otp", "booking_confirmation", "payment_receipt", "cancellation_notice", "reminder"] }, data: { type: "object", description: "Type-specific payload; e.g., {otp_code} for otp, {appointment_time, smb_name} for booking_confirmation" }, preferred_channel: { type: "string", enum: ["sms", "email", "auto"], default: "sms" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_message", unit_price_usd: 0.02 }, slo: { p50_ms: 500, p95_ms: 2e3 }, idempotency: "required \u2014 Idempotency-Key prevents double-sends of OTPs and confirmations", failure_modes: ["bad_input", "upstream_failure", "supply_unreachable"], examples: [{ label: "Booking confirmation SMS", input: { recipient: { phone_or_email: "+14045551234", name: "Alex" }, confirmation_type: "booking_confirmation", data: { appointment_time: "Tuesday April 29 at 10:30am", smb_name: "Cuts & Co.", address: "123 Main St, Atlanta" } }, output: { status: "success", channel_used: "sms:twilio" } }], user_query_examples: [{ user_says: "Send the booking confirmation receipt to my email", agent_call: { tool: "send_transactional_confirmation", arguments: { recipient_id: "user@example.com", channel_preference: "email", confirmation_type: "booking" } } }] }, { name: "handle_inbound", description: "Receive, classify, and route inbound messages on behalf of an SMB. Classifies intent (booking request, cancellation, inquiry, complaint), enriches with context, and routes to the appropriate handler or escalation path.", when_to_use: "Use when an SMB needs inbound message triage \u2014 classifying incoming contact-form submissions, SMS replies, voicemails, or email inquiries.", when_not_to_use: "Do not use for outbound communications. Do not use for compliance-flagged recipient lists without verified opt-in records.", execution_profile: "async_by_default", compliance_constraints: ["Inbound message content may contain PII \u2014 retained per jurisdiction retention policy.", "GDPR data subject deletion requests received via inbound must be escalated immediately."], input_schema: { type: "object", required: ["smb_id", "inbound_channel", "raw_message"], properties: { smb_id: { type: "string" }, inbound_channel: { type: "string", enum: ["sms", "email", "voice_voicemail", "web_form", "api"] }, sender: { type: "object", properties: { phone: { type: "string" }, email: { type: "string" }, name: { type: "string" } } }, raw_message: { type: "string" }, received_at_iso: { type: "string", format: "date-time" }, routing_rules: { type: "object", description: "Optional override routing policy for this SMB" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_inbound", unit_price_usd: 0.03 }, slo: { p50_ms: 3e3, p95_ms: 15e3 }, idempotency: "required", failure_modes: ["bad_input", "upstream_failure", "transient"], examples: [{ label: "Inbound SMS booking request triaged", input: { smb_id: "smb_001", inbound_channel: "sms", sender: { phone: "+14045551234" }, raw_message: "Hi, do you have anything Saturday morning?", received_at_iso: "2026-04-27T14:00:00Z" }, output: { status: "success", result: { classified_intent: "booking_inquiry", suggested_action: "check_availability", routed_to: "schedule_appointment_flow", enriched_sender: { known_customer: true, last_booking: "2026-03-15" } } } }], user_query_examples: [{ user_says: "Process this customer reply for me: 'Yes I want to book Tuesday'", agent_call: { tool: "handle_inbound", arguments: { raw_message: "Yes I want to book Tuesday", channel: "sms" } } }] }, { name: "escalate_to_human", description: "Hand off an in-flight task to a human operator with a full context bundle: transcript, prior actions, identifiers, and a recommended next step.", when_to_use: "Use when automated resolution has failed after channel-fallback exhaustion, when the task requires human judgment, or when the customer has explicitly requested human contact.", when_not_to_use: "Do not use as a first resort. Escalate only after automated resolution attempts.", execution_profile: "async_by_default", compliance_constraints: ["Context bundle may contain PII \u2014 handled per jurisdiction retention policy.", "Recording transcripts included in handoff must have recording consent confirmed."], input_schema: { type: "object", required: ["smb_id", "reason", "context"], properties: { smb_id: { type: "string" }, reason: { type: "string", enum: ["automation_failed", "customer_requested", "compliance_hold", "ambiguous_intent", "exception_required"] }, context: { type: "object", properties: { original_operation: { type: "string" }, operation_id: { type: "string" }, transcript: { type: "array", items: { type: "object" } }, prior_actions: { type: "array", items: { type: "object" } }, recommended_next_step: { type: "string" } } }, priority: { type: "string", enum: ["normal", "urgent"], default: "normal" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_escalation", unit_price_usd: 0.2 }, slo: { p50_ms: 2e3, p95_ms: 1e4 }, idempotency: "required", failure_modes: ["bad_input", "supply_unreachable", "upstream_failure"], examples: [{ label: "Escalation after voice AI booking failure", input: { smb_id: "smb_044", reason: "automation_failed", context: { original_operation: "schedule_appointment", operation_id: "op_fail_001", recommended_next_step: "Call the business directly at their listed number to confirm the emergency plumbing slot" } }, output: { status: "success", result: { escalation_ticket_id: "esc_777", assigned_queue: "smb_support" } } }], user_query_examples: [{ user_says: "I'm stuck \u2014 get a human at smb_xyz to call me back", agent_call: { tool: "escalate_to_human", arguments: { smb_id: "smb_xyz", reason: "agent_blocked", summary: "Cannot resolve via automated channels" } } }] }, { name: "get_status", description: "Query the current state of any in-flight async operation by operation_id.", when_to_use: "Use to poll the state of a pending_async operation when no webhook callback has arrived or to check progress.", when_not_to_use: "Do not poll more frequently than once per 10 seconds \u2014 use webhook delivery for real-time updates instead.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } }, output_schema: { type: "object", properties: { operation_id: { type: "string" }, status: { type: "string", enum: ["pending", "executing", "success", "failure", "partial"] }, estimated_completion_time: { type: "string", format: "date-time" }, last_updated_at: { type: "string", format: "date-time" }, partial_result: { type: "object" } } }, cost_model: { basis: "per_call", unit_price_usd: 1e-3 }, slo: { p50_ms: 50, p95_ms: 200 }, idempotency: "read-only", failure_modes: ["bad_input", "transient"], examples: [{ label: "Booking still in progress", input: { operation_id: "op_xyz789" }, output: { status: "executing", estimated_completion_time: "2026-04-27T10:01:30Z" } }] }, { name: "get_outcome", description: "Retrieve the final OutcomeReceipt for a completed operation.", when_to_use: "Use after get_status returns success/failure/partial to retrieve the full result with cost and reason codes.", when_not_to_use: "Do not use for operations still in pending/executing state \u2014 use get_status first.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_call", unit_price_usd: 1e-3 }, slo: { p50_ms: 50, p95_ms: 200 }, idempotency: "read-only", failure_modes: ["bad_input", "transient"], examples: [{ label: "Retrieve booking outcome", input: { operation_id: "op_xyz789" }, output: { status: "success", reason_code: "appointment_confirmed", result: { appointment_id: "appt_cal_9001", confirmed_time: "2026-04-29T10:30:00Z", smb_name: "Cuts & Co." }, cost: { amount: 1, currency: "USD", basis: "per_booking_attempt+success_bonus" }, channel_used: "direct_api:square" } }] }, { name: "preview_cost", description: "Return an expected cost estimate, latency estimate, and success-probability estimate for a proposed call before execution. Accuracy SLO: actual cost within \xB15% of preview.", when_to_use: "Use before any operation when the agent is operating under a budget constraint and needs to decide whether to proceed.", when_not_to_use: "Do not use in a hot loop \u2014 cache the result for at least 60 seconds if repeating the same preview.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["operation", "params"], properties: { operation: { type: "string" }, params: { type: "object", description: "The same request body you would pass to the operation" } } }, output_schema: { type: "object", properties: { estimated_cost_usd: { type: "number" }, cost_range: { type: "object", properties: { min_usd: { type: "number" }, max_usd: { type: "number" } } }, estimated_latency_p50_ms: { type: "integer" }, estimated_latency_p95_ms: { type: "integer" }, success_probability_estimate: { type: "number", minimum: 0, maximum: 1 }, channel_likely: { type: "string" }, cost_accuracy_slo: { type: "string", const: "\xB15%" } } }, cost_model: { basis: "per_call", unit_price_usd: 1e-3 }, slo: { p50_ms: 100, p95_ms: 500 }, idempotency: "read-only", failure_modes: ["bad_input", "transient"], examples: [{ label: "Preview appointment booking cost", input: { operation: "schedule_appointment", params: { smb_id: "smb_001", action: "book", service: "haircut" } }, output: { estimated_cost_usd: 1, cost_range: { min_usd: 0.25, max_usd: 1 }, estimated_latency_p50_ms: 5e3, success_probability_estimate: 0.88, channel_likely: "direct_api:square", cost_accuracy_slo: "\xB15%" } }], user_query_examples: [{ user_says: "How much will this SMS cost me?", agent_call: { tool: "preview_cost", arguments: { operation: "send_message", params: { channel_preference: "sms" } } } }, { user_says: "Estimate the cost of booking via voice fallback", agent_call: { tool: "preview_cost", arguments: { operation: "schedule_appointment" } } }] }, { name: "self_test", description: "Live capability probe that verifies the service is healthy, each claimed operation is reachable, and supply network size is current. Use to verify integration before production use.", when_to_use: "Use at agent startup, before high-stakes task sequences, or after receiving unexpected errors to check if the service is degraded.", when_not_to_use: "Do not call more than once per minute in production.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", properties: {} }, output_schema: { type: "object", properties: { healthy: { type: "boolean" }, capabilities_verified: { type: "array", items: { type: "string" } }, version: { type: "string" }, supply_network_size: { type: "integer" }, channel_status: { type: "object" }, degraded_channels: { type: "array", items: { type: "string" } } } }, cost_model: { basis: "free" }, slo: { p50_ms: 200, p95_ms: 1e3 }, idempotency: "read-only", failure_modes: ["transient", "internal"], examples: [{ label: "Healthy service response", input: {}, output: { healthy: true, capabilities_verified: ["find_business", "schedule_appointment", "send_message", "preview_cost"], version: "0.1.0", supply_network_size: 47, channel_status: { sms: "operational", voice_ai: "operational", direct_api: "operational" }, degraded_channels: [] } }], user_query_examples: [{ user_says: "Run a health check before I send the broadcast", agent_call: { tool: "self_test", arguments: {} } }] }, { name: "check_booking_link", description: "Free, instant pre-flight check for a booking URL. Classifies which booking platform a URL belongs to and tells you whether import_booking_url will accept it, WITHOUT fetching the page or spending money. Returns the platform, the exact smb_id import_booking_url would assign, the channels the booking will route through, and the inferred country. Use it to de-risk a paid booking BEFORE calling import_booking_url + schedule_appointment.", when_to_use: "Call this the moment a user pastes a URL and you are not sure it is a bookable page, or before you commit to a paid schedule_appointment. It is free and sub-100ms, so run it as a guard: if supported=true, proceed to import_booking_url with confidence; if supported=false, fall back to find_business or call_business instead of wasting a booking attempt.", when_not_to_use: "Do not use to confirm the page is currently live/available \u2014 this tool does not fetch the URL, it only classifies its shape. It is not a substitute for import_booking_url (which actually registers the business) or verify_business (which confirms an already-imported smb_id).", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["url"], properties: { url: { type: "string", description: "Full http(s) URL to classify, e.g. 'https://cal.com/jane' or 'https://www.opentable.com/r/acme'." } } }, output_schema: { type: "object", properties: { supported: { type: "boolean", description: "True if the URL host is a supported booking platform." }, importable: { type: "boolean", description: "True if import_booking_url will accept this URL (mirrors its host allowlist)." }, platform: { type: "string", description: "Detected platform (e.g. 'cal.com', 'calendly', 'doctolib') or 'unknown'." }, predicted_smb_id: { type: "string", description: "The idempotent smb_id import_booking_url would assign to this URL." }, expected_channels: { type: "array", items: { type: "string" } }, expected_capabilities: { type: "array", items: { type: "string" } }, inferred_country: { type: "string" }, checked_live: { type: "boolean", description: "Always false \u2014 this is an offline classification, the page is never fetched." } } }, cost_model: { basis: "free" }, slo: { p50_ms: 20, p95_ms: 100 }, idempotency: "read-only", failure_modes: ["bad_input"], examples: [{ label: "Supported Cal.com link", input: { url: "https://cal.com/jane" }, output: { supported: true, importable: true, platform: "cal.com", predicted_smb_id: "smb_imp_...", expected_channels: ["direct_api:calcom"], checked_live: false } }, { label: "Unsupported generic URL", input: { url: "https://example.com/book-now" }, output: { supported: false, importable: false, platform: "unknown" } }], user_query_examples: [{ user_says: "Is this a bookable link? https://cal.com/jane", agent_call: { tool: "check_booking_link", arguments: { url: "https://cal.com/jane" } }, then_call: { tool: "import_booking_url", arguments: { booking_url: "https://cal.com/jane" } } }, { user_says: "Can you book me here: https://www.opentable.com/r/acme-bistro", agent_call: { tool: "check_booking_link", arguments: { url: "https://www.opentable.com/r/acme-bistro" } } }] }, { name: "import_booking_url", description: "Turn ANY public booking URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) into a callable smb_id you can immediately use with schedule_appointment, send_message, or capture_lead. Idempotent \u2014 calling twice returns the same smb_id.", when_to_use: "Call this FIRST whenever the user provides a specific booking URL (cal.com/handle, calendly.com/handle/event, doctolib.fr/..., booksy.com/..., opentable.com/r/..., etc.). User patterns that match: 'book me at https://cal.com/...', 'schedule with calendly.com/jane/intro', 'reserve a table at opentable.com/r/...', 'I want to book this dentist: https://www.doctolib.fr/...'. After importing, the returned smb_id can be passed straight to schedule_appointment.", when_not_to_use: "Do not use if the user only describes a business by name without a URL \u2014 call find_business instead. Do not use for arbitrary websites that are not on the supported booking-platform list (use /supply/platforms to see all 12).", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["booking_url"], properties: { booking_url: { type: "string", format: "uri", description: "Full URL the user supplied. Must point at one of the 12 supported booking platforms; auto-detected from the host." }, business_name: { type: "string", description: "Optional override. If omitted, the business name is auto-extracted from the page's <title> or og:title." }, vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services", "restaurants", "retail", "healthcare", "fitness"], description: "Best-guess vertical. If omitted, inferred from the platform (e.g., Doctolib -> healthcare, OpenTable -> restaurants)." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'FR'). Used for compliance routing on later send_message calls." }, contact_phone: { type: "string", description: "Optional. If omitted, the platform integration handles outreach." }, contact_email: { type: "string", description: "Optional." }, capabilities: { type: "array", items: { type: "string" }, description: "Free-form capability tags (e.g., ['haircut','color','blowdry'])." } } }, output_schema: { type: "object", required: ["status", "smb_id"], properties: { status: { type: "string", enum: ["success", "duplicate", "fail"] }, smb_id: { type: "string", description: "Stable identifier \u2014 pass to schedule_appointment, send_message, or capture_lead next." }, platform: { type: "string", description: "Detected platform name." }, message: { type: "string" }, next_steps: { type: "array", items: { type: "string" } } } }, cost_model: { basis: "per_call", amount_usd: 5e-3 }, slo: { p50_latency_ms: 600, max_latency_ms: 3e3 }, examples: [{ user_says: "Book me a haircut at https://cal.com/jane-salon", agent_call: { tool: "import_booking_url", arguments: { booking_url: "https://cal.com/jane-salon", vertical: "personal_services" } }, then_call: { tool: "schedule_appointment", arguments: { smb_id: "<from_above>", preferred_time: "user-specified" } } }, { user_says: "I want to see Dr. Dupont \u2014 https://www.doctolib.fr/dentiste/paris/jean-dupont", agent_call: { tool: "import_booking_url", arguments: { booking_url: "https://www.doctolib.fr/dentiste/paris/jean-dupont" } } }], user_query_examples: [{ user_says: "Book me a haircut at https://cal.com/jane-salon", agent_call: { tool: "import_booking_url", arguments: { booking_url: "https://cal.com/jane-salon", vertical: "personal_services" } }, then_call: { tool: "schedule_appointment", arguments: { smb_id: "<from_above>", action: "book" } } }, { user_says: "Schedule with this dentist: https://www.doctolib.fr/dentiste/paris/jean-dupont", agent_call: { tool: "import_booking_url", arguments: { booking_url: "https://www.doctolib.fr/dentiste/paris/jean-dupont" } } }, { user_says: "Reserve a table at https://www.opentable.com/r/acme-bistro", agent_call: { tool: "import_booking_url", arguments: { booking_url: "https://www.opentable.com/r/acme-bistro", vertical: "restaurants" } } }], idempotency: { key_scope: ["agent_id", "booking_url"], ttl_seconds: 86400, behavior: "Calling import_booking_url twice with the same URL returns the same smb_id (no duplicate). Safe to retry." }, failure_modes: [{ reason_code: "platform_not_supported", retriable: false, description: "Host did not match any of the 12 supported booking platforms." }, { reason_code: "url_not_reachable", retriable: true, description: "The page returned 404 / 403 / timeout. Retry after fixing the URL." }, { reason_code: "page_metadata_extraction_failed", retriable: false, description: "Page exists but we could not extract a usable business name. Pass `business_name` explicitly." }] }, { name: "call_business", description: "Place a conversational voice-AI phone call to a business on a consumer's behalf and return a structured answer. THE differentiated capability: reach the ~60M long-tail SMBs that have NO API and NO booking page \u2014 only a phone number. An AI agent cannot pick up a phone and hold a conversation; this tool does. Give a plain-language objective; the voice AI navigates the call and extracts the answer. Business-directed (B2B), far less restricted than calling consumers \u2014 but the compliance gate still enforces recording consent per jurisdiction. Async: returns a call handle; poll get_outcome for the transcript + extracted fields.", when_to_use: "Use when the target business has NO booking URL and NO API \u2014 only a phone number \u2014 and the consumer asked the agent to reach them (e.g. 'call this plumber and ask if they can come Tuesday', 'ask the salon if they take walk-ins this afternoon'). Also use to confirm details a booking page doesn't expose (real-time availability, custom quotes).", when_not_to_use: "Do NOT use when the business has a booking URL \u2014 use import_booking_url + schedule_appointment (cheaper, faster, deterministic). Do NOT use for calls to consumers/individuals (this tool is for reaching businesses). Do NOT use for marketing or telemarketing \u2014 the compliance gate and the B2B-only framing reject that.", execution_profile: "async_by_default", compliance_constraints: ["Business-directed voice call. The opening line identifies the caller as an AI assistant acting on a consumer's behalf.", "Recording consent enforced per jurisdiction (two-party-consent states/countries get a spoken consent prompt before recording).", "Not for consumer-directed autodialing or prerecorded marketing (TCPA) \u2014 objective must be an operational business question."], input_schema: { type: "object", required: ["objective"], properties: { business_phone: { type: "string", description: "Business phone in E.164 (e.g. +14045550123). Provide this OR smb_id." }, smb_id: { type: "string", description: "Known SMB identifier with a phone on record. Provide this OR business_phone." }, objective: { type: "string", description: "What the call should accomplish, in plain language." }, extract_fields: { type: "array", items: { type: "string" }, description: "Structured fields to pull from the answer, e.g. ['available_tomorrow','price_quote','earliest_slot']." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 for compliance + recording-consent routing." }, on_behalf_of: { type: "string", description: "Name of the consumer the call is placed for." }, max_duration_seconds: { type: "integer", maximum: 600, default: 180 } } }, output_schema: { $ref: "#/components/OutcomeReceipt" }, cost_model: { basis: "per_call", unit_price_usd: 0.5 }, slo: { p50_ms: 45e3, p95_ms: 18e4 }, idempotency: "NOT idempotent \u2014 each call places a new phone call. Do not retry a call_business that returned pending_async; poll get_outcome instead.", failure_modes: ["bad_input", "voice_not_provisioned", "compliance_violation", "upstream_failure"], examples: [{ label: "Call a plumber with no booking page", input: { business_phone: "+14045550142", objective: "Ask if they can do an emergency drain unclog tomorrow morning and roughly what it costs.", extract_fields: ["available_tomorrow_am", "price_estimate"], country_code: "US", on_behalf_of: "the customer at 14 Maple St" }, output: { status: "pending_async", reason_code: "call_placed", channel_used: "voice_ai:vapi", cost: { amount: 0.5, currency: "USD" } } }] }, { name: "check_compliance", description: "Free, instant pre-flight for the compliance gate. Runs the SAME TCPA / GDPR / CASL / CAN-SPAM / 10DLC gate that send_message and call_business run \u2014 but in preview mode, so NO message is sent and NO state changes. Tells you whether a (recipient, channel, message_type, content) send would be permitted BEFORE you pay for it, and if not, names the exact rule and how to remediate. Use it to de-risk a paid send the same way check_booking_link de-risks a paid booking.", when_to_use: "Call this the moment before send_message or call_business when there is any chance the send is regulated \u2014 anything tagged marketing, any SMS to a US number (10DLC), any message to an EU/UK (GDPR) or Canadian (CASL) recipient, or any content you are unsure about. It is free and sub-100ms, so run it as a guard: if legal=true, proceed to send_message with confidence; if legal=false, fix the cited blocker instead of burning a paid, rejected send.", when_not_to_use: "Do not treat a legal=true as a permanent license \u2014 the gate re-runs at send time, so a fresh opt-out between preview and send still blocks. Do not use it to check two-party voice recording consent (that is evaluated at call time in the voice adapter, not here). It is not a substitute for send_message; it never delivers anything.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["recipient_id", "content"], properties: { recipient_id: { type: "string", description: "Phone in E.164 (e.g. '+14045550100') or email address the message would go to." }, content: { type: "string", description: "The actual message body you intend to send. The gate classifies the real text, so a meaningful preview needs the real content." }, channel: { type: "string", enum: ["sms", "email", "voice"], description: "Delivery channel. Omit to auto-infer sms/email from recipient_id; set 'voice' explicitly." }, message_type: { type: "string", description: "Intent tag: transactional, marketing, reminder, follow_up, notification. 'marketing' triggers the consent checks. Defaults to transactional.", default: "transactional" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'DE', 'CA'). Auto-inferred from phone if omitted; drives which jurisdiction rules apply." }, state_code: { type: "string", description: "US state code (e.g. 'CA') for state-specific rules." } } }, output_schema: { type: "object", properties: { legal: { type: "boolean", description: "True if the gate would permit this send." }, channel: { type: "string" }, message_type: { type: "string" }, jurisdiction: { type: "string" }, rule: { type: "string", description: "The rule that blocked the send (null when legal=true)." }, remediation: { type: "string", description: "How to become compliant (present when legal=false)." }, checked_live: { type: "boolean", description: "Always false \u2014 this is a gate decision preview, not a live delivery." } } }, cost_model: { basis: "free" }, slo: { p50_ms: 15, p95_ms: 80 }, idempotency: "read-only \u2014 no send, no audit write, no idempotency key required", failure_modes: ["bad_input"], examples: [{ label: "Transactional email is permitted", input: { recipient_id: "jane@example.com", content: "Your appointment at Cuts & Co. is confirmed for Tuesday 10:30am.", message_type: "transactional", country_code: "US" }, output: { status: "success", reason_code: "compliant", result: { legal: true, channel: "email", message_type: "transactional", checked_live: false } } }, { label: "Marketing SMS blocked \u2014 no recorded consent", input: { recipient_id: "+14045550200", content: "20% off this week only!", channel: "sms", message_type: "marketing", country_code: "US" }, output: { status: "success", reason_code: "not_compliant", result: { legal: false, rule: "TCPA_marketing_consent", remediation: "Obtain prior express written consent (TCPA) before sending marketing SMS to US numbers, and pass its consent_record_id.", checked_live: false } } }], user_query_examples: [{ user_says: "Is it legal to text this US number a 20%-off promo?", agent_call: { tool: "check_compliance", arguments: { recipient_id: "+14045550200", content: "20% off this week only!", channel: "sms", message_type: "marketing", country_code: "US" } } }, { user_says: "Before you email the dentist, make sure it's allowed", agent_call: { tool: "check_compliance", arguments: { recipient_id: "office@dentist.example", content: "Do you accept Cigna? Following up on my request.", message_type: "follow_up" } }, then_call: { tool: "send_message", arguments: { recipient: { id_type: "email", id_value: "office@dentist.example" }, message_type: "follow_up", content: { body: "Do you accept Cigna? Following up on my request." } } } }] }, { name: "verify_company_record", description: "Free, live lookup of a company official registry record. Queries the GLEIF global LEI registry (primary, 2.6 million legal entities worldwide) and SEC EDGAR (US public companies) to return the official legal name, LEI, entity status, jurisdiction, registered address, and registry authority. Never fabricates: if the company is not found in these free registries, returns an honest not_found with the sources that were queried.", when_to_use: "Use when you need to verify that a company exists as a registered legal entity and retrieve its official registry details -- before signing a contract, qualifying a vendor, validating a counterparty, or populating a due-diligence record. Accepts a legal name plus optional country filter or a direct LEI for a precise lookup.", when_not_to_use: "Do not use to verify private companies not registered with GLEIF or SEC. Do not use as an exhaustive fraud-detection tool; this is a first-pass existence check against free public registries, not a full KYC screen.", execution_profile: "sync", compliance_constraints: [], input_schema: { type: "object", required: ["name"], properties: { name: { type: "string", description: "Legal company name to look up, e.g. Apple Inc or Volkswagen AG." }, country: { type: "string", description: "Optional ISO 3166-1 alpha-2 country filter (e.g. US, DE, GB). Narrows GLEIF results to one jurisdiction." }, lei: { type: "string", description: "Optional 20-character Legal Entity Identifier for a direct, precise lookup." } } }, output_schema: { type: "object", properties: { status: { type: "string", enum: ["found", "not_found"] }, legal_name: { type: "string" }, lei: { type: "string" }, entity_status: { type: "string" }, jurisdiction: { type: "string" }, registered_address: { type: "string" }, registry_authority: { type: "string" }, ticker: { type: "string" }, sec_cik: { type: "string" }, sources: { type: "array", items: { type: "string" } }, sources_queried: { type: "array", items: { type: "string" } }, sources_unavailable: { type: "array", items: { type: "string" } } } }, cost_model: { basis: "free" }, slo: { p50_ms: 800, p95_ms: 4e3 }, idempotency: "read-only", failure_modes: ["bad_input", "upstream_timeout"], examples: [{ label: "Look up Apple Inc by name and country", input: { name: "Apple Inc", country: "US" }, output: { status: "found", legal_name: "Apple Inc.", lei: "HWUPKR0MPOU8FGXBT394", entity_status: "ACTIVE", jurisdiction: "US", sources: ["GLEIF", "SEC EDGAR"] } }, { label: "Direct LEI lookup", input: { name: "Volkswagen AG", lei: "529900HNOAA1KXQJUQ27" }, output: { status: "found", legal_name: "Volkswagen AG", lei: "529900HNOAA1KXQJUQ27", entity_status: "ACTIVE", jurisdiction: "DE", sources: ["GLEIF"] } }, { label: "Company not found in registries", input: { name: "zzz-nonexistent-company-xyz" }, output: { status: "not_found" } }], user_query_examples: [{ user_says: "Is Apple Inc a real registered company?", agent_call: { tool: "verify_company_record", arguments: { name: "Apple Inc", country: "US" } } }, { user_says: "Look up the LEI for Volkswagen AG", agent_call: { tool: "verify_company_record", arguments: { name: "Volkswagen AG", country: "DE" } } }, { user_says: "Verify this LEI: 529900HNOAA1KXQJUQ27", agent_call: { tool: "verify_company_record", arguments: { name: "Volkswagen AG", lei: "529900HNOAA1KXQJUQ27" } } }] }], components: { OutcomeReceipt: { type: "object", properties: { operation_id: { type: "string" }, status: { type: "string", enum: ["success", "partial", "failure", "pending_async"] }, reason_code: { type: "string" }, human_message: { type: "string" }, result: { type: "object" }, cost: { type: "object", properties: { amount: { type: "number" }, currency: { type: "string" }, basis: { type: "string" } } }, latency_ms: { type: "integer" }, channel_used: { type: "string" }, channel_fallback_chain: { type: "array", items: { type: "string" } }, estimated_completion_time: { type: "string", format: "date-time" }, next_actions: { type: "array", items: { type: "string" } }, retriable: { type: "boolean" }, trace_id: { type: "string" } } } }, manifest_version: "0.1.0", generated_at: "2026-04-27T00:00:00Z", slo_data_freshness: "static_seed \u2014 replace with telemetry-driven values at P8" };

// src/snapshots/agents.json
var agents_default = { name: "SMB Transaction & Communication Broker", description: "Agent-callable service for the long tail of small businesses. Discover, verify, communicate, schedule, transact \u2014 all with built-in TCPA/GDPR/CASL compliance and idempotent semantics.", version: "0.1.0", protocol_version: "a2a-v0.2", url: "https://smb-broker.onrender.com", documentation_url: "https://smb-broker.onrender.com/docs", default_input_modes: ["application/json"], default_output_modes: ["application/json"], capabilities: { streaming: true, push_notifications: true, state_transition_history: true }, authentication: { schemes: ["bearer", "agent-identity-jwt"], header: "X-Agent-Identity", token_endpoint: "https://smb-broker.onrender.com/auth/token" }, skills: [{ id: "find_business", name: "Find Business", description: "Given criteria (vertical, location, capability, price band, availability window), return ranked candidate SMBs from the verified supply network. Returns only curated, verified, transactable businesses \u2014 not raw directory results.", tags: ["sync", "read_only"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "verify_business", name: "Verify Business", description: "Confirm that an SMB is real, currently operating, and capable of the requested service. Performs a live capability probe against the business's channel.", tags: ["sync", "read_only"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "send_message", name: "Send Message", description: "Send a message on behalf of an agent's user or an SMB across SMS, email, or voice. Five message types: transactional, reminder, follow_up, notification, marketing. Every send routes through a non-bypassable compliance gate (TCPA, GDPR, CASL, PDPL across 22 jurisdictions) that enforces opt-in consent for marketing/promotional content \u2014 marketing without recorded consent is rejected at runtime with a structured compliance_violation receipt. Channel is abstracted: specify intent and recipient; the service selects and falls back across channels.", tags: ["sync_fast", "write", "compliance_gated"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "capture_lead", name: "Capture Lead", description: "Structured intake of a prospect into an SMB's funnel with validation, enrichment hooks, and deduplication. Inserts into the SMB's CRM or direct-booking pipeline if available.", tags: ["sync_fast", "write", "compliance_gated"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "schedule_appointment", name: "Schedule Appointment", description: "Availability lookup, hold, confirm, reschedule, or cancel appointments with an SMB. Routes through the SMB's native booking system if available, falls back to voice AI or web form.", tags: ["async_by_default", "write", "compliance_gated"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "send_transactional_confirmation", name: "Send Transactional Confirmation", description: "Idempotent transactional messages: OTPs, booking confirmations, payment receipts, cancellation notices. Guaranteed delivery via redundant channels.", tags: ["sync_fast", "write", "compliance_gated"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "handle_inbound", name: "Handle Inbound", description: "Receive, classify, and route inbound messages on behalf of an SMB. Classifies intent (booking request, cancellation, inquiry, complaint), enriches with context, and routes to the appropriate handler or escalation path.", tags: ["async_by_default", "compliance_gated"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "escalate_to_human", name: "Escalate To Human", description: "Hand off an in-flight task to a human operator with a full context bundle: transcript, prior actions, identifiers, and a recommended next step.", tags: ["async_by_default", "compliance_gated"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "get_status", name: "Get Status", description: "Query the current state of any in-flight async operation by operation_id.", tags: ["sync"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "get_outcome", name: "Get Outcome", description: "Retrieve the final OutcomeReceipt for a completed operation.", tags: ["sync"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "preview_cost", name: "Preview Cost", description: "Return an expected cost estimate, latency estimate, and success-probability estimate for a proposed call before execution. Accuracy SLO: actual cost within \xB15% of preview.", tags: ["sync", "read_only"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "self_test", name: "Self Test", description: "Live capability probe that verifies the service is healthy, each claimed operation is reachable, and supply network size is current. Use to verify integration before production use.", tags: ["sync"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "check_booking_link", name: "Check Booking Link", description: "Free, instant pre-flight check for a booking URL. Classifies which booking platform a URL belongs to and tells you whether import_booking_url will accept it, WITHOUT fetching the page or spending money. Returns the platform, the exact smb_id import_booking_url would assign, the channels the booking will route through, and the inferred country. Use it to de-risk a paid booking BEFORE calling import_booking_url + schedule_appointment.", tags: ["sync"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "import_booking_url", name: "Import Booking Url", description: "Turn ANY public booking URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) into a callable smb_id you can immediately use with schedule_appointment, send_message, or capture_lead. Idempotent \u2014 calling twice returns the same smb_id.", tags: ["sync"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "call_business", name: "Call Business", description: "Place a conversational voice-AI phone call to a business on a consumer's behalf and return a structured answer. THE differentiated capability: reach the ~60M long-tail SMBs that have NO API and NO booking page \u2014 only a phone number. An AI agent cannot pick up a phone and hold a conversation; this tool does. Give a plain-language objective; the voice AI navigates the call and extracts the answer. Business-directed (B2B), far less restricted than calling consumers \u2014 but the compliance gate still enforces recording consent per jurisdiction. Async: returns a call handle; poll get_outcome for the transcript + extracted fields.", tags: ["async_by_default", "compliance_gated"], examples: [""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "check_compliance", name: "Check Compliance", description: "Free, instant pre-flight for the compliance gate. Runs the SAME TCPA / GDPR / CASL / CAN-SPAM / 10DLC gate that send_message and call_business run \u2014 but in preview mode, so NO message is sent and NO state changes. Tells you whether a (recipient, channel, message_type, content) send would be permitted BEFORE you pay for it, and if not, names the exact rule and how to remediate. Use it to de-risk a paid send the same way check_booking_link de-risks a paid booking.", tags: ["sync"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }, { id: "verify_company_record", name: "Verify Company Record", description: "Free, live lookup of a company official registry record. Queries the GLEIF global LEI registry (primary, 2.6 million legal entities worldwide) and SEC EDGAR (US public companies) to return the official legal name, LEI, entity status, jurisdiction, registered address, and registry authority. Never fabricates: if the company is not found in these free registries, returns an honest not_found with the sources that were queried.", tags: ["sync", "read_only"], examples: ["", ""], input_modes: ["application/json"], output_modes: ["application/json"] }], supported_protocols: ["mcp", "openai-tools", "anthropic-tools", "rest", "a2a"], discovery_urls: { mcp: "https://smb-broker.onrender.com/mcp", openapi: "https://smb-broker.onrender.com/openapi.yaml", manifest: "https://smb-broker.onrender.com/manifest", ai_plugin: "https://smb-broker.onrender.com/.well-known/ai-plugin.json" } };

// src/snapshots/ai-plugin.json
var ai_plugin_default = { schema_version: "v1", name_for_human: "Agent Broker", name_for_model: "agent_broker", description_for_human: "Discover, verify, message, and schedule with millions of small businesses through a single compliance-aware API.", description_for_model: "Plugin for AI agents to interact with small/mid-sized businesses (SMBs) \u2014 the long tail of local services. Capabilities: find_business (search by vertical+location+capability), verify_business (confirm capabilities), send_message (SMS/email/voice with full TCPA/GDPR compliance), capture_lead, schedule_appointment (Cal.com direct booking \u2192 voice fallback), send_transactional_confirmation, handle_inbound (classify customer messages), escalate_to_human, get_status, get_outcome, preview_cost (free), self_test (free). ALWAYS call preview_cost before any state-changing operation. Always pass an X-Agent-Identity header for state-changing ops. WinRate is the north-star metric.", auth: { type: "user_http", authorization_type: "bearer", verification_tokens: {} }, api: { type: "openapi", url: "https://smb-broker.onrender.com/openapi.yaml", is_user_authenticated: true }, logo_url: "https://smb-broker.onrender.com/static/logo.png", contact_email: "basilalshukaili@gmail.com", legal_info_url: "https://smb-broker.onrender.com/legal" };

// src/snapshots/openai-tools.json
var openai_tools_default = { version: "1.0", service: "agent-broker", tools: [{ type: "function", function: { name: "find_business", description: "Given criteria (vertical, location, capability, price band, availability window), return ranked candidate SMBs from the verified supply network. Returns only curated, verified, transactable businesses \u2014 not raw directory results. Use when: Use when an agent needs to identify which SMBs can fulfill a business task (booking, service, consultation) in a given location and vertical. Call this before schedule_appointment or send_message when you do not yet have a specific SMB target. Do NOT use when: Do not use as a general directory or browsing surface. Do not use when you already have a specific verified SMB identifier. Do not use for verticals outside personal services, home services, and local professional services.", parameters: { type: "object", required: ["vertical", "location"], properties: { vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services"], description: "Service vertical to search within" }, location: { type: "object", required: ["zip_or_city"], properties: { zip_or_city: { type: "string" }, radius_miles: { type: "number", default: 10 } } }, capability: { type: "string", description: "Specific service capability required, e.g. 'haircut', 'plumbing', 'tax_consultation'" }, price_band: { type: "object", properties: { max_usd: { type: "number" } } }, availability_window: { type: "object", properties: { start_iso: { type: "string", format: "date-time" }, end_iso: { type: "string", format: "date-time" } } }, max_results: { type: "integer", default: 5, maximum: 20 } } } } }, { type: "function", function: { name: "verify_business", description: "Confirm that an SMB is real, currently operating, and capable of the requested service. Performs a live capability probe against the business's channel. Use when: Use before sending communications or scheduling if you have an unverified SMB identifier, or if the agent's task requires confirmed capability (e.g., 'I need to be sure they do emergency plumbing'). Do NOT use when: Do not use if the SMB was returned from find_business within the last 24 hours \u2014 those results are already verified.", parameters: { type: "object", required: ["smb_id"], properties: { smb_id: { type: "string" }, capability_to_verify: { type: "string" } } } } }, { type: "function", function: { name: "send_message", description: "Send a message on behalf of an agent's user or an SMB across SMS, email, or voice. Five message types: transactional, reminder, follow_up, notification, marketing. Every send routes through a non-bypassable compliance gate (TCPA, GDPR, CASL, PDPL across 22 jurisdictions) that enforces opt-in consent for marketing/promotional content \u2014 marketing without recorded consent is rejected at runtime with a structured compliance_violation receipt. Channel is abstracted: specify intent and recipient; the service selects and falls back across channels. Use when: Use to: (a) confirm a booking the agent just made, (b) reply to a customer who messaged the SMB first, (c) follow up on a quote the user requested, (d) send appointment reminders the SMB owes its customer, (e) send marketing messages to recipients who have opted in (with consent_record_id). The gate verifies consent on every send. Do NOT use when: Do NOT use for OTPs or critical transactional confirmations \u2014 use send_transactional_confirmation. Do NOT attempt to send marketing without a consent_record_id pointing at a real opt-in \u2014 the gate will reject the send and log a compliance_violation. Do NOT attempt bulk / list-based / drip / cold outreach \u2014 those are out of scope and the rate limiter will throttle abuse. Execution: sync_fast \u2014 returns pending_async.", parameters: { type: "object", required: ["recipient", "message_type", "content"], properties: { recipient: { type: "object", required: ["id_type", "id_value"], properties: { id_type: { type: "string", enum: ["phone", "email", "smb_id", "customer_id"] }, id_value: { type: "string" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2, required for compliance routing" } } }, message_type: { type: "string", description: "Intent tag for the message. Five permitted types. 'marketing' is allowed only when paired with a valid consent_record_id; the compliance gate verifies the consent at send time and rejects (compliance_violation receipt) if it's missing, expired, or revoked.", enum: ["transactional", "marketing", "reminder", "follow_up", "notification"] }, content: { type: "object", required: ["body"], properties: { body: { type: "string" }, subject: { type: "string", description: "For email channel" }, template_id: { type: "string" }, template_vars: { type: "object" } } }, preferred_channel: { type: "string", enum: ["sms", "email", "voice", "auto"], default: "auto" }, send_at_iso: { type: "string", format: "date-time", description: "Schedule for future delivery; omit for immediate" } } } } }, { type: "function", function: { name: "capture_lead", description: "Structured intake of a prospect into an SMB's funnel with validation, enrichment hooks, and deduplication. Inserts into the SMB's CRM or direct-booking pipeline if available. Use when: Use when a potential customer has expressed interest in an SMB's service and you want to ensure they are registered in the SMB's pipeline for follow-up. Do NOT use when: Do not use for confirmed bookings \u2014 use schedule_appointment. Do not use for bulk list imports. Execution: sync_fast \u2014 returns pending_async.", parameters: { type: "object", required: ["smb_id", "prospect"], properties: { smb_id: { type: "string" }, prospect: { type: "object", required: ["name"], properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string", format: "email" }, service_interest: { type: "string" }, notes: { type: "string" }, consent_record_id: { type: "string", description: "Optional ID of a consent record proving the prospect asked to be contacted (e.g., they filled an SMB's intake form or requested a quote). Required when downstream send_message calls are anticipated." } } }, source: { type: "string", description: "Where the consumer-initiated request originated (e.g., 'consumer_request', 'inbound_quote_form', 'agent_referral_from_find_business')." } } } } }, { type: "function", function: { name: "schedule_appointment", description: "Availability lookup, hold, confirm, reschedule, or cancel appointments with an SMB. Routes through the SMB's native booking system if available, falls back to voice AI or web form. Use when: Use when an agent needs to book, reschedule, or cancel a specific appointment with a specific SMB. Requires a verified smb_id. Do NOT use when: Do not use for bulk scheduling. Do not use without a verified SMB \u2014 call find_business and verify_business first if needed. Execution: async_by_default \u2014 returns pending_async.", parameters: { type: "object", required: ["smb_id", "action"], properties: { smb_id: { type: "string" }, action: { type: "string", enum: ["book", "reschedule", "cancel", "check_availability"] }, service: { type: "string" }, customer: { type: "object", properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string" } } }, requested_time: { type: "object", properties: { preferred_iso: { type: "string", format: "date-time" }, window_start_iso: { type: "string", format: "date-time" }, window_end_iso: { type: "string", format: "date-time" }, duration_minutes: { type: "integer" } } }, existing_appointment_id: { type: "string", description: "Required for reschedule/cancel" }, notes: { type: "string" } } } } }, { type: "function", function: { name: "send_transactional_confirmation", description: "Idempotent transactional messages: OTPs, booking confirmations, payment receipts, cancellation notices. Guaranteed delivery via redundant channels. Use when: Use for any message that MUST be delivered reliably \u2014 OTPs, booking confirmations, receipts. Do not use for marketing. Do NOT use when: Do not use for marketing or promotional messages. Do not use for conversational messages. Execution: sync_fast \u2014 returns pending_async.", parameters: { type: "object", required: ["recipient", "confirmation_type", "data"], properties: { recipient: { type: "object", required: ["phone_or_email"], properties: { phone_or_email: { type: "string" }, name: { type: "string" } } }, confirmation_type: { type: "string", enum: ["otp", "booking_confirmation", "payment_receipt", "cancellation_notice", "reminder"] }, data: { type: "object", description: "Type-specific payload; e.g., {otp_code} for otp, {appointment_time, smb_name} for booking_confirmation" }, preferred_channel: { type: "string", enum: ["sms", "email", "auto"], default: "sms" } } } } }, { type: "function", function: { name: "handle_inbound", description: "Receive, classify, and route inbound messages on behalf of an SMB. Classifies intent (booking request, cancellation, inquiry, complaint), enriches with context, and routes to the appropriate handler or escalation path. Use when: Use when an SMB needs inbound message triage \u2014 classifying incoming contact-form submissions, SMS replies, voicemails, or email inquiries. Do NOT use when: Do not use for outbound communications. Do not use for compliance-flagged recipient lists without verified opt-in records. Execution: async_by_default \u2014 returns pending_async.", parameters: { type: "object", required: ["smb_id", "inbound_channel", "raw_message"], properties: { smb_id: { type: "string" }, inbound_channel: { type: "string", enum: ["sms", "email", "voice_voicemail", "web_form", "api"] }, sender: { type: "object", properties: { phone: { type: "string" }, email: { type: "string" }, name: { type: "string" } } }, raw_message: { type: "string" }, received_at_iso: { type: "string", format: "date-time" }, routing_rules: { type: "object", description: "Optional override routing policy for this SMB" } } } } }, { type: "function", function: { name: "escalate_to_human", description: "Hand off an in-flight task to a human operator with a full context bundle: transcript, prior actions, identifiers, and a recommended next step. Use when: Use when automated resolution has failed after channel-fallback exhaustion, when the task requires human judgment, or when the customer has explicitly requested human contact. Do NOT use when: Do not use as a first resort. Escalate only after automated resolution attempts. Execution: async_by_default \u2014 returns pending_async.", parameters: { type: "object", required: ["smb_id", "reason", "context"], properties: { smb_id: { type: "string" }, reason: { type: "string", enum: ["automation_failed", "customer_requested", "compliance_hold", "ambiguous_intent", "exception_required"] }, context: { type: "object", properties: { original_operation: { type: "string" }, operation_id: { type: "string" }, transcript: { type: "array", items: { type: "object" } }, prior_actions: { type: "array", items: { type: "object" } }, recommended_next_step: { type: "string" } } }, priority: { type: "string", enum: ["normal", "urgent"], default: "normal" } } } } }, { type: "function", function: { name: "get_status", description: "Query the current state of any in-flight async operation by operation_id. Use when: Use to poll the state of a pending_async operation when no webhook callback has arrived or to check progress. Do NOT use when: Do not poll more frequently than once per 10 seconds \u2014 use webhook delivery for real-time updates instead.", parameters: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } } } }, { type: "function", function: { name: "get_outcome", description: "Retrieve the final OutcomeReceipt for a completed operation. Use when: Use after get_status returns success/failure/partial to retrieve the full result with cost and reason codes. Do NOT use when: Do not use for operations still in pending/executing state \u2014 use get_status first.", parameters: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } } } }, { type: "function", function: { name: "preview_cost", description: "Return an expected cost estimate, latency estimate, and success-probability estimate for a proposed call before execution. Accuracy SLO: actual cost within \xB15% of preview. Use when: Use before any operation when the agent is operating under a budget constraint and needs to decide whether to proceed. Do NOT use when: Do not use in a hot loop \u2014 cache the result for at least 60 seconds if repeating the same preview.", parameters: { type: "object", required: ["operation", "params"], properties: { operation: { type: "string" }, params: { type: "object", description: "The same request body you would pass to the operation" } } } } }, { type: "function", function: { name: "self_test", description: "Live capability probe that verifies the service is healthy, each claimed operation is reachable, and supply network size is current. Use to verify integration before production use. Use when: Use at agent startup, before high-stakes task sequences, or after receiving unexpected errors to check if the service is degraded. Do NOT use when: Do not call more than once per minute in production.", parameters: { type: "object", properties: {} } } }, { type: "function", function: { name: "check_booking_link", description: "Free, instant pre-flight check for a booking URL. Classifies which booking platform a URL belongs to and tells you whether import_booking_url will accept it, WITHOUT fetching the page or spending money. Returns the platform, the exact smb_id import_booking_url would assign, the channels the booking will route through, and the inferred country. Use it to de-risk a paid booking BEFORE calling import_booking_url + schedule_appointment. Use when: Call this the moment a user pastes a URL and you are not sure it is a bookable page, or before you commit to a paid schedule_appointment. It is free and sub-100ms, so run it as a guard: if supported=true, proceed to import_booking_url with confidence; if supported=false, fall back to find_business or call_business instead of wasting a booking attempt. Do NOT use when: Do not use to confirm the page is currently live/available \u2014 this tool does not fetch the URL, it only classifies its shape. It is not a substitute for import_booking_url (which actually registers the business) or verify_business (which confirms an already-imported smb_id).", parameters: { type: "object", required: ["url"], properties: { url: { type: "string", description: "Full http(s) URL to classify, e.g. 'https://cal.com/jane' or 'https://www.opentable.com/r/acme'." } } } } }, { type: "function", function: { name: "import_booking_url", description: "Turn ANY public booking URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) into a callable smb_id you can immediately use with schedule_appointment, send_message, or capture_lead. Idempotent \u2014 calling twice returns the same smb_id. Use when: Call this FIRST whenever the user provides a specific booking URL (cal.com/handle, calendly.com/handle/event, doctolib.fr/..., booksy.com/..., opentable.com/r/..., etc.). User patterns that match: 'book me at https://cal.com/...', 'schedule with calendly.com/jane/intro', 'reserve a table at opentable.com/r/...', 'I want to book this dentist: https://www.doctolib.fr/...'. After importing, the returned smb_id can be passed straight to schedule_appointment. Do NOT use when: Do not use if the user only describes a business by name without a URL \u2014 call find_business instead. Do not use for arbitrary websites that are not on the supported booking-platform list (use /supply/platforms to see all 12). Cost: ~$0.005 per call.", parameters: { type: "object", required: ["booking_url"], properties: { booking_url: { type: "string", format: "uri", description: "Full URL the user supplied. Must point at one of the 12 supported booking platforms; auto-detected from the host." }, business_name: { type: "string", description: "Optional override. If omitted, the business name is auto-extracted from the page's <title> or og:title." }, vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services", "restaurants", "retail", "healthcare", "fitness"], description: "Best-guess vertical. If omitted, inferred from the platform (e.g., Doctolib -> healthcare, OpenTable -> restaurants)." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'FR'). Used for compliance routing on later send_message calls." }, contact_phone: { type: "string", description: "Optional. If omitted, the platform integration handles outreach." }, contact_email: { type: "string", description: "Optional." }, capabilities: { type: "array", items: { type: "string" }, description: "Free-form capability tags (e.g., ['haircut','color','blowdry'])." } } } } }, { type: "function", function: { name: "call_business", description: "Place a conversational voice-AI phone call to a business on a consumer's behalf and return a structured answer. THE differentiated capability: reach the ~60M long-tail SMBs that have NO API and NO booking page \u2014 only a phone number. An AI agent cannot pick up a phone and hold a conversation; this tool does. Give a plain-language objective; the voice AI navigates the call and extracts the answer. Business-directed (B2B), far less restricted than calling consumers \u2014 but the compliance gate still enforces recording consent per jurisdiction. Async: returns a call handle; poll get_outcome for the transcript + extracted fields. Use when: Use when the target business has NO booking URL and NO API \u2014 only a phone number \u2014 and the consumer asked the agent to reach them (e.g. 'call this plumber and ask if they can come Tuesday', 'ask the salon if they take walk-ins this afternoon'). Also use to confirm details a booking page doesn't expose (real-time availability, custom quotes). Do NOT use when: Do NOT use when the business has a booking URL \u2014 use import_booking_url + schedule_appointment (cheaper, faster, deterministic). Do NOT use for calls to consumers/individuals (this tool is for reaching businesses). Do NOT use for marketing or telemarketing \u2014 the compliance gate and the B2B-only framing reject that. Execution: async_by_default \u2014 returns pending_async.", parameters: { type: "object", required: ["objective"], properties: { business_phone: { type: "string", description: "Business phone in E.164 (e.g. +14045550123). Provide this OR smb_id." }, smb_id: { type: "string", description: "Known SMB identifier with a phone on record. Provide this OR business_phone." }, objective: { type: "string", description: "What the call should accomplish, in plain language." }, extract_fields: { type: "array", items: { type: "string" }, description: "Structured fields to pull from the answer, e.g. ['available_tomorrow','price_quote','earliest_slot']." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 for compliance + recording-consent routing." }, on_behalf_of: { type: "string", description: "Name of the consumer the call is placed for." }, max_duration_seconds: { type: "integer", maximum: 600, default: 180 } } } } }, { type: "function", function: { name: "check_compliance", description: "Free, instant pre-flight for the compliance gate. Runs the SAME TCPA / GDPR / CASL / CAN-SPAM / 10DLC gate that send_message and call_business run \u2014 but in preview mode, so NO message is sent and NO state changes. Tells you whether a (recipient, channel, message_type, content) send would be permitted BEFORE you pay for it, and if not, names the exact rule and how to remediate. Use it to de-risk a paid send the same way check_booking_link de-risks a paid booking. Use when: Call this the moment before send_message or call_business when there is any chance the send is regulated \u2014 anything tagged marketing, any SMS to a US number (10DLC), any message to an EU/UK (GDPR) or Canadian (CASL) recipient, or any content you are unsure about. It is free and sub-100ms, so run it as a guard: if legal=true, proceed to send_message with confidence; if legal=false, fix the cited blocker instead of burning a paid, rejected send. Do NOT use when: Do not treat a legal=true as a permanent license \u2014 the gate re-runs at send time, so a fresh opt-out between preview and send still blocks. Do not use it to check two-party voice recording consent (that is evaluated at call time in the voice adapter, not here). It is not a substitute for send_message; it never delivers anything.", parameters: { type: "object", required: ["recipient_id", "content"], properties: { recipient_id: { type: "string", description: "Phone in E.164 (e.g. '+14045550100') or email address the message would go to." }, content: { type: "string", description: "The actual message body you intend to send. The gate classifies the real text, so a meaningful preview needs the real content." }, channel: { type: "string", enum: ["sms", "email", "voice"], description: "Delivery channel. Omit to auto-infer sms/email from recipient_id; set 'voice' explicitly." }, message_type: { type: "string", description: "Intent tag: transactional, marketing, reminder, follow_up, notification. 'marketing' triggers the consent checks. Defaults to transactional.", default: "transactional" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'DE', 'CA'). Auto-inferred from phone if omitted; drives which jurisdiction rules apply." }, state_code: { type: "string", description: "US state code (e.g. 'CA') for state-specific rules." } } } } }, { type: "function", function: { name: "verify_company_record", description: "Free, live lookup of a company official registry record. Queries the GLEIF global LEI registry (primary, 2.6 million legal entities worldwide) and SEC EDGAR (US public companies) to return the official legal name, LEI, entity status, jurisdiction, registered address, and registry authority. Never fabricates: if the company is not found in these free registries, returns an honest not_found with the sources that were queried. Use when: Use when you need to verify that a company exists as a registered legal entity and retrieve its official registry details -- before signing a contract, qualifying a vendor, validating a counterparty, or populating a due-diligence record. Accepts a legal name plus optional country filter or a direct LEI for a precise lookup. Do NOT use when: Do not use to verify private companies not registered with GLEIF or SEC. Do not use as an exhaustive fraud-detection tool; this is a first-pass existence check against free public registries, not a full KYC screen.", parameters: { type: "object", required: ["name"], properties: { name: { type: "string", description: "Legal company name to look up, e.g. Apple Inc or Volkswagen AG." }, country: { type: "string", description: "Optional ISO 3166-1 alpha-2 country filter (e.g. US, DE, GB). Narrows GLEIF results to one jurisdiction." }, lei: { type: "string", description: "Optional 20-character Legal Entity Identifier for a direct, precise lookup." } } } } }], endpoint: "https://smb-broker.onrender.com/ops/{tool_name}", auth_header: "X-Agent-Identity" };

// src/snapshots/anthropic-tools.json
var anthropic_tools_default = { version: "1.0", service: "agent-broker", tools: [{ name: "find_business", description: "Given criteria (vertical, location, capability, price band, availability window), return ranked candidate SMBs from the verified supply network. Returns only curated, verified, transactable businesses \u2014 not raw directory results. Use when: Use when an agent needs to identify which SMBs can fulfill a business task (booking, service, consultation) in a given location and vertical. Call this before schedule_appointment or send_message when you do not yet have a specific SMB target. Do NOT use when: Do not use as a general directory or browsing surface. Do not use when you already have a specific verified SMB identifier. Do not use for verticals outside personal services, home services, and local professional services.", input_schema: { type: "object", required: ["vertical", "location"], properties: { vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services"], description: "Service vertical to search within" }, location: { type: "object", required: ["zip_or_city"], properties: { zip_or_city: { type: "string" }, radius_miles: { type: "number", default: 10 } } }, capability: { type: "string", description: "Specific service capability required, e.g. 'haircut', 'plumbing', 'tax_consultation'" }, price_band: { type: "object", properties: { max_usd: { type: "number" } } }, availability_window: { type: "object", properties: { start_iso: { type: "string", format: "date-time" }, end_iso: { type: "string", format: "date-time" } } }, max_results: { type: "integer", default: 5, maximum: 20 } } } }, { name: "verify_business", description: "Confirm that an SMB is real, currently operating, and capable of the requested service. Performs a live capability probe against the business's channel. Use when: Use before sending communications or scheduling if you have an unverified SMB identifier, or if the agent's task requires confirmed capability (e.g., 'I need to be sure they do emergency plumbing'). Do NOT use when: Do not use if the SMB was returned from find_business within the last 24 hours \u2014 those results are already verified.", input_schema: { type: "object", required: ["smb_id"], properties: { smb_id: { type: "string" }, capability_to_verify: { type: "string" } } } }, { name: "send_message", description: "Send a message on behalf of an agent's user or an SMB across SMS, email, or voice. Five message types: transactional, reminder, follow_up, notification, marketing. Every send routes through a non-bypassable compliance gate (TCPA, GDPR, CASL, PDPL across 22 jurisdictions) that enforces opt-in consent for marketing/promotional content \u2014 marketing without recorded consent is rejected at runtime with a structured compliance_violation receipt. Channel is abstracted: specify intent and recipient; the service selects and falls back across channels. Use when: Use to: (a) confirm a booking the agent just made, (b) reply to a customer who messaged the SMB first, (c) follow up on a quote the user requested, (d) send appointment reminders the SMB owes its customer, (e) send marketing messages to recipients who have opted in (with consent_record_id). The gate verifies consent on every send. Do NOT use when: Do NOT use for OTPs or critical transactional confirmations \u2014 use send_transactional_confirmation. Do NOT attempt to send marketing without a consent_record_id pointing at a real opt-in \u2014 the gate will reject the send and log a compliance_violation. Do NOT attempt bulk / list-based / drip / cold outreach \u2014 those are out of scope and the rate limiter will throttle abuse. Execution: sync_fast \u2014 returns pending_async.", input_schema: { type: "object", required: ["recipient", "message_type", "content"], properties: { recipient: { type: "object", required: ["id_type", "id_value"], properties: { id_type: { type: "string", enum: ["phone", "email", "smb_id", "customer_id"] }, id_value: { type: "string" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2, required for compliance routing" } } }, message_type: { type: "string", description: "Intent tag for the message. Five permitted types. 'marketing' is allowed only when paired with a valid consent_record_id; the compliance gate verifies the consent at send time and rejects (compliance_violation receipt) if it's missing, expired, or revoked.", enum: ["transactional", "marketing", "reminder", "follow_up", "notification"] }, content: { type: "object", required: ["body"], properties: { body: { type: "string" }, subject: { type: "string", description: "For email channel" }, template_id: { type: "string" }, template_vars: { type: "object" } } }, preferred_channel: { type: "string", enum: ["sms", "email", "voice", "auto"], default: "auto" }, send_at_iso: { type: "string", format: "date-time", description: "Schedule for future delivery; omit for immediate" } } } }, { name: "capture_lead", description: "Structured intake of a prospect into an SMB's funnel with validation, enrichment hooks, and deduplication. Inserts into the SMB's CRM or direct-booking pipeline if available. Use when: Use when a potential customer has expressed interest in an SMB's service and you want to ensure they are registered in the SMB's pipeline for follow-up. Do NOT use when: Do not use for confirmed bookings \u2014 use schedule_appointment. Do not use for bulk list imports. Execution: sync_fast \u2014 returns pending_async.", input_schema: { type: "object", required: ["smb_id", "prospect"], properties: { smb_id: { type: "string" }, prospect: { type: "object", required: ["name"], properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string", format: "email" }, service_interest: { type: "string" }, notes: { type: "string" }, consent_record_id: { type: "string", description: "Optional ID of a consent record proving the prospect asked to be contacted (e.g., they filled an SMB's intake form or requested a quote). Required when downstream send_message calls are anticipated." } } }, source: { type: "string", description: "Where the consumer-initiated request originated (e.g., 'consumer_request', 'inbound_quote_form', 'agent_referral_from_find_business')." } } } }, { name: "schedule_appointment", description: "Availability lookup, hold, confirm, reschedule, or cancel appointments with an SMB. Routes through the SMB's native booking system if available, falls back to voice AI or web form. Use when: Use when an agent needs to book, reschedule, or cancel a specific appointment with a specific SMB. Requires a verified smb_id. Do NOT use when: Do not use for bulk scheduling. Do not use without a verified SMB \u2014 call find_business and verify_business first if needed. Execution: async_by_default \u2014 returns pending_async.", input_schema: { type: "object", required: ["smb_id", "action"], properties: { smb_id: { type: "string" }, action: { type: "string", enum: ["book", "reschedule", "cancel", "check_availability"] }, service: { type: "string" }, customer: { type: "object", properties: { name: { type: "string" }, phone: { type: "string" }, email: { type: "string" } } }, requested_time: { type: "object", properties: { preferred_iso: { type: "string", format: "date-time" }, window_start_iso: { type: "string", format: "date-time" }, window_end_iso: { type: "string", format: "date-time" }, duration_minutes: { type: "integer" } } }, existing_appointment_id: { type: "string", description: "Required for reschedule/cancel" }, notes: { type: "string" } } } }, { name: "send_transactional_confirmation", description: "Idempotent transactional messages: OTPs, booking confirmations, payment receipts, cancellation notices. Guaranteed delivery via redundant channels. Use when: Use for any message that MUST be delivered reliably \u2014 OTPs, booking confirmations, receipts. Do not use for marketing. Do NOT use when: Do not use for marketing or promotional messages. Do not use for conversational messages. Execution: sync_fast \u2014 returns pending_async.", input_schema: { type: "object", required: ["recipient", "confirmation_type", "data"], properties: { recipient: { type: "object", required: ["phone_or_email"], properties: { phone_or_email: { type: "string" }, name: { type: "string" } } }, confirmation_type: { type: "string", enum: ["otp", "booking_confirmation", "payment_receipt", "cancellation_notice", "reminder"] }, data: { type: "object", description: "Type-specific payload; e.g., {otp_code} for otp, {appointment_time, smb_name} for booking_confirmation" }, preferred_channel: { type: "string", enum: ["sms", "email", "auto"], default: "sms" } } } }, { name: "handle_inbound", description: "Receive, classify, and route inbound messages on behalf of an SMB. Classifies intent (booking request, cancellation, inquiry, complaint), enriches with context, and routes to the appropriate handler or escalation path. Use when: Use when an SMB needs inbound message triage \u2014 classifying incoming contact-form submissions, SMS replies, voicemails, or email inquiries. Do NOT use when: Do not use for outbound communications. Do not use for compliance-flagged recipient lists without verified opt-in records. Execution: async_by_default \u2014 returns pending_async.", input_schema: { type: "object", required: ["smb_id", "inbound_channel", "raw_message"], properties: { smb_id: { type: "string" }, inbound_channel: { type: "string", enum: ["sms", "email", "voice_voicemail", "web_form", "api"] }, sender: { type: "object", properties: { phone: { type: "string" }, email: { type: "string" }, name: { type: "string" } } }, raw_message: { type: "string" }, received_at_iso: { type: "string", format: "date-time" }, routing_rules: { type: "object", description: "Optional override routing policy for this SMB" } } } }, { name: "escalate_to_human", description: "Hand off an in-flight task to a human operator with a full context bundle: transcript, prior actions, identifiers, and a recommended next step. Use when: Use when automated resolution has failed after channel-fallback exhaustion, when the task requires human judgment, or when the customer has explicitly requested human contact. Do NOT use when: Do not use as a first resort. Escalate only after automated resolution attempts. Execution: async_by_default \u2014 returns pending_async.", input_schema: { type: "object", required: ["smb_id", "reason", "context"], properties: { smb_id: { type: "string" }, reason: { type: "string", enum: ["automation_failed", "customer_requested", "compliance_hold", "ambiguous_intent", "exception_required"] }, context: { type: "object", properties: { original_operation: { type: "string" }, operation_id: { type: "string" }, transcript: { type: "array", items: { type: "object" } }, prior_actions: { type: "array", items: { type: "object" } }, recommended_next_step: { type: "string" } } }, priority: { type: "string", enum: ["normal", "urgent"], default: "normal" } } } }, { name: "get_status", description: "Query the current state of any in-flight async operation by operation_id. Use when: Use to poll the state of a pending_async operation when no webhook callback has arrived or to check progress. Do NOT use when: Do not poll more frequently than once per 10 seconds \u2014 use webhook delivery for real-time updates instead.", input_schema: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } } }, { name: "get_outcome", description: "Retrieve the final OutcomeReceipt for a completed operation. Use when: Use after get_status returns success/failure/partial to retrieve the full result with cost and reason codes. Do NOT use when: Do not use for operations still in pending/executing state \u2014 use get_status first.", input_schema: { type: "object", required: ["operation_id"], properties: { operation_id: { type: "string" } } } }, { name: "preview_cost", description: "Return an expected cost estimate, latency estimate, and success-probability estimate for a proposed call before execution. Accuracy SLO: actual cost within \xB15% of preview. Use when: Use before any operation when the agent is operating under a budget constraint and needs to decide whether to proceed. Do NOT use when: Do not use in a hot loop \u2014 cache the result for at least 60 seconds if repeating the same preview.", input_schema: { type: "object", required: ["operation", "params"], properties: { operation: { type: "string" }, params: { type: "object", description: "The same request body you would pass to the operation" } } } }, { name: "self_test", description: "Live capability probe that verifies the service is healthy, each claimed operation is reachable, and supply network size is current. Use to verify integration before production use. Use when: Use at agent startup, before high-stakes task sequences, or after receiving unexpected errors to check if the service is degraded. Do NOT use when: Do not call more than once per minute in production.", input_schema: { type: "object", properties: {} } }, { name: "check_booking_link", description: "Free, instant pre-flight check for a booking URL. Classifies which booking platform a URL belongs to and tells you whether import_booking_url will accept it, WITHOUT fetching the page or spending money. Returns the platform, the exact smb_id import_booking_url would assign, the channels the booking will route through, and the inferred country. Use it to de-risk a paid booking BEFORE calling import_booking_url + schedule_appointment. Use when: Call this the moment a user pastes a URL and you are not sure it is a bookable page, or before you commit to a paid schedule_appointment. It is free and sub-100ms, so run it as a guard: if supported=true, proceed to import_booking_url with confidence; if supported=false, fall back to find_business or call_business instead of wasting a booking attempt. Do NOT use when: Do not use to confirm the page is currently live/available \u2014 this tool does not fetch the URL, it only classifies its shape. It is not a substitute for import_booking_url (which actually registers the business) or verify_business (which confirms an already-imported smb_id).", input_schema: { type: "object", required: ["url"], properties: { url: { type: "string", description: "Full http(s) URL to classify, e.g. 'https://cal.com/jane' or 'https://www.opentable.com/r/acme'." } } } }, { name: "import_booking_url", description: "Turn ANY public booking URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) into a callable smb_id you can immediately use with schedule_appointment, send_message, or capture_lead. Idempotent \u2014 calling twice returns the same smb_id. Use when: Call this FIRST whenever the user provides a specific booking URL (cal.com/handle, calendly.com/handle/event, doctolib.fr/..., booksy.com/..., opentable.com/r/..., etc.). User patterns that match: 'book me at https://cal.com/...', 'schedule with calendly.com/jane/intro', 'reserve a table at opentable.com/r/...', 'I want to book this dentist: https://www.doctolib.fr/...'. After importing, the returned smb_id can be passed straight to schedule_appointment. Do NOT use when: Do not use if the user only describes a business by name without a URL \u2014 call find_business instead. Do not use for arbitrary websites that are not on the supported booking-platform list (use /supply/platforms to see all 12). Cost: ~$0.005 per call.", input_schema: { type: "object", required: ["booking_url"], properties: { booking_url: { type: "string", format: "uri", description: "Full URL the user supplied. Must point at one of the 12 supported booking platforms; auto-detected from the host." }, business_name: { type: "string", description: "Optional override. If omitted, the business name is auto-extracted from the page's <title> or og:title." }, vertical: { type: "string", enum: ["personal_services", "home_services", "professional_services", "restaurants", "retail", "healthcare", "fitness"], description: "Best-guess vertical. If omitted, inferred from the platform (e.g., Doctolib -> healthcare, OpenTable -> restaurants)." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'FR'). Used for compliance routing on later send_message calls." }, contact_phone: { type: "string", description: "Optional. If omitted, the platform integration handles outreach." }, contact_email: { type: "string", description: "Optional." }, capabilities: { type: "array", items: { type: "string" }, description: "Free-form capability tags (e.g., ['haircut','color','blowdry'])." } } } }, { name: "call_business", description: "Place a conversational voice-AI phone call to a business on a consumer's behalf and return a structured answer. THE differentiated capability: reach the ~60M long-tail SMBs that have NO API and NO booking page \u2014 only a phone number. An AI agent cannot pick up a phone and hold a conversation; this tool does. Give a plain-language objective; the voice AI navigates the call and extracts the answer. Business-directed (B2B), far less restricted than calling consumers \u2014 but the compliance gate still enforces recording consent per jurisdiction. Async: returns a call handle; poll get_outcome for the transcript + extracted fields. Use when: Use when the target business has NO booking URL and NO API \u2014 only a phone number \u2014 and the consumer asked the agent to reach them (e.g. 'call this plumber and ask if they can come Tuesday', 'ask the salon if they take walk-ins this afternoon'). Also use to confirm details a booking page doesn't expose (real-time availability, custom quotes). Do NOT use when: Do NOT use when the business has a booking URL \u2014 use import_booking_url + schedule_appointment (cheaper, faster, deterministic). Do NOT use for calls to consumers/individuals (this tool is for reaching businesses). Do NOT use for marketing or telemarketing \u2014 the compliance gate and the B2B-only framing reject that. Execution: async_by_default \u2014 returns pending_async.", input_schema: { type: "object", required: ["objective"], properties: { business_phone: { type: "string", description: "Business phone in E.164 (e.g. +14045550123). Provide this OR smb_id." }, smb_id: { type: "string", description: "Known SMB identifier with a phone on record. Provide this OR business_phone." }, objective: { type: "string", description: "What the call should accomplish, in plain language." }, extract_fields: { type: "array", items: { type: "string" }, description: "Structured fields to pull from the answer, e.g. ['available_tomorrow','price_quote','earliest_slot']." }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 for compliance + recording-consent routing." }, on_behalf_of: { type: "string", description: "Name of the consumer the call is placed for." }, max_duration_seconds: { type: "integer", maximum: 600, default: 180 } } } }, { name: "check_compliance", description: "Free, instant pre-flight for the compliance gate. Runs the SAME TCPA / GDPR / CASL / CAN-SPAM / 10DLC gate that send_message and call_business run \u2014 but in preview mode, so NO message is sent and NO state changes. Tells you whether a (recipient, channel, message_type, content) send would be permitted BEFORE you pay for it, and if not, names the exact rule and how to remediate. Use it to de-risk a paid send the same way check_booking_link de-risks a paid booking. Use when: Call this the moment before send_message or call_business when there is any chance the send is regulated \u2014 anything tagged marketing, any SMS to a US number (10DLC), any message to an EU/UK (GDPR) or Canadian (CASL) recipient, or any content you are unsure about. It is free and sub-100ms, so run it as a guard: if legal=true, proceed to send_message with confidence; if legal=false, fix the cited blocker instead of burning a paid, rejected send. Do NOT use when: Do not treat a legal=true as a permanent license \u2014 the gate re-runs at send time, so a fresh opt-out between preview and send still blocks. Do not use it to check two-party voice recording consent (that is evaluated at call time in the voice adapter, not here). It is not a substitute for send_message; it never delivers anything.", input_schema: { type: "object", required: ["recipient_id", "content"], properties: { recipient_id: { type: "string", description: "Phone in E.164 (e.g. '+14045550100') or email address the message would go to." }, content: { type: "string", description: "The actual message body you intend to send. The gate classifies the real text, so a meaningful preview needs the real content." }, channel: { type: "string", enum: ["sms", "email", "voice"], description: "Delivery channel. Omit to auto-infer sms/email from recipient_id; set 'voice' explicitly." }, message_type: { type: "string", description: "Intent tag: transactional, marketing, reminder, follow_up, notification. 'marketing' triggers the consent checks. Defaults to transactional.", default: "transactional" }, country_code: { type: "string", description: "ISO 3166-1 alpha-2 (e.g. 'US', 'DE', 'CA'). Auto-inferred from phone if omitted; drives which jurisdiction rules apply." }, state_code: { type: "string", description: "US state code (e.g. 'CA') for state-specific rules." } } } }, { name: "verify_company_record", description: "Free, live lookup of a company official registry record. Queries the GLEIF global LEI registry (primary, 2.6 million legal entities worldwide) and SEC EDGAR (US public companies) to return the official legal name, LEI, entity status, jurisdiction, registered address, and registry authority. Never fabricates: if the company is not found in these free registries, returns an honest not_found with the sources that were queried. Use when: Use when you need to verify that a company exists as a registered legal entity and retrieve its official registry details -- before signing a contract, qualifying a vendor, validating a counterparty, or populating a due-diligence record. Accepts a legal name plus optional country filter or a direct LEI for a precise lookup. Do NOT use when: Do not use to verify private companies not registered with GLEIF or SEC. Do not use as an exhaustive fraud-detection tool; this is a first-pass existence check against free public registries, not a full KYC screen.", input_schema: { type: "object", required: ["name"], properties: { name: { type: "string", description: "Legal company name to look up, e.g. Apple Inc or Volkswagen AG." }, country: { type: "string", description: "Optional ISO 3166-1 alpha-2 country filter (e.g. US, DE, GB). Narrows GLEIF results to one jurisdiction." }, lei: { type: "string", description: "Optional 20-character Legal Entity Identifier for a direct, precise lookup." } } } }], endpoint: "https://smb-broker.onrender.com/ops/{tool_name}", auth_header: "X-Agent-Identity", mcp_endpoint: "https://smb-broker.onrender.com/mcp" };

// src/snapshots/mcp.json
var mcp_default = {
  name: "agent-broker",
  version: "0.1.0",
  transport: {
    type: "streamable-http",
    endpoint: "https://agent-broker-edge.basil-agent.workers.dev/mcp",
    method: "POST",
    content_type: "application/json"
  },
  payments: {
    status: "coming_soon",
    note: "Per-call micropayment billing (x402/USDC on Base) is in development and not yet active. All tools are currently free to call. This field will describe payment requirements once billing goes live."
  },
  description: "MCP server for SMB Transaction & Communication Broker. Exposes 14 operations: find_business, verify_business, send_message, capture_lead, schedule_appointment, send_transactional_confirmation, handle_inbound, escalate_to_human, get_status, get_outcome, preview_cost, self_test, import_booking_url, call_business.",
  auth: {
    header: "X-Agent-Identity",
    scheme: "bearer"
  }
};

// src/snapshots/agent-service.json
var agent_service_default = { service_type: "smb_broker", service_id: "smb-broker-v1", version: "0.1.0", description: "SMB Transaction & Communication Broker \u2014 enables AI agents to discover, verify, communicate with, and schedule appointments with long-tail small and mid-sized businesses through a single, compliance-aware tool surface.", auth: { scheme: "AgentIdentity", header: "X-Agent-Identity", token_url: "/auth/token", token_format: "HS256 signed claims (stub) \u2014 use issue_token()" }, manifest_url: "/manifest", operations_url: "/manifest/ops", health_url: "/health", contact: { support_email: "support@agent-broker-edge.basil-agent.workers.dev", docs_url: "https://agent-broker-edge.basil-agent.workers.dev/docs", openapi_url: "https://agent-broker-edge.basil-agent.workers.dev/openapi.yaml", mcp_tools_url: "https://agent-broker-edge.basil-agent.workers.dev/manifest/mcp_tools.json" }, verticals_supported: ["personal_services", "home_services", "professional_services"], geo_coverage: ["US"], compliance: { tcpa: true, gdpr: true, casl: true, can_spam: true, "10dlc": true, recording_consent: true }, execution_profiles: { sync: "\u22642s response", sync_fast: "\u22645s response", async_by_default: "returns pending_async, completes via webhook" } };

// src/snapshots/supply-platforms.json
var supply_platforms_default = { platforms: [{ name: "Cal.com", pattern: "https://cal.com/<handle>", example: "https://cal.com/peer" }, { name: "Calendly", pattern: "https://calendly.com/<handle>/<slug>", example: "https://calendly.com/acme/intro" }, { name: "Doctolib", pattern: "https://www.doctolib.{fr,de,it}/<specialty>/<city>/<doctor>", example: "https://www.doctolib.fr/dentiste/paris/jean-dupont" }, { name: "Booksy", pattern: "https://booksy.com/en-us/<id>_<slug>", example: "https://booksy.com/en-us/123_jane-salon" }, { name: "Fresha", pattern: "https://fresha.com/<slug>", example: "https://fresha.com/a/jane-salon-london" }, { name: "OpenTable", pattern: "https://www.opentable.com/r/<slug>", example: "https://www.opentable.com/r/acme-bistro-tokyo" }, { name: "Setmore", pattern: "https://setmore.com/<slug>", example: "https://setmore.com/jane-salon" }, { name: "Square", pattern: "https://squareup.com/appointments/book/<id> or https://<name>.square.site", example: "https://jane.square.site" }, { name: "Acuity", pattern: "https://app.acuityscheduling.com/schedule.php?owner=<id>", example: "https://app.acuityscheduling.com/schedule.php?owner=12345" }, { name: "Schedulista", pattern: "https://www.schedulista.com/<slug>", example: "https://www.schedulista.com/jane-salon" }, { name: "Squarespace", pattern: "https://<custom>.squarespace-scheduling.com", example: "https://jane.squarespace-scheduling.com" }, { name: "BookMyCity", pattern: "https://bookmycity.com/<slug>", example: "https://bookmycity.com/jane-salon" }], import_endpoint: "/supply/import_booking_url", next_step: "Pick a URL the user mentioned, POST it to /supply/import_booking_url, then the directory is populated and find_business returns the business. The schedule_appointment call works against the imported entry." };

// src/snapshots/jurisdictions.json
var jurisdictions_default = { supported: ["AE", "AU", "BH", "BR", "CA", "DE", "ES", "EU", "FR", "GB", "ID", "IN", "INTERNATIONAL", "IT", "JP", "KR", "KW", "MX", "NL", "NZ", "OM", "PK", "QA", "SA", "SG", "US"], fallback: "international", note: "Unknown jurisdictions fall back to a conservative INTERNATIONAL rule set." };

// src/snapshots/mcp-tools-list.json
var mcp_tools_list_default = {
  result: {
    tools: [
      {
        name: "find_business",
        description: 'Given criteria (vertical, location, capability, price band, availability window), return ranked candidate SMBs from the verified supply network. Returns only curated, verified, transactable businesses \u2014 not raw directory results.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Find me a salon in Tokyo that does color"\n  -> call find_business({"vertical": "personal_services", "location": {"zip_or_city": "Tokyo"}, "capability": "color"})\n  user: "I need a plumber near 30309"\n  -> call find_business({"vertical": "home_services", "location": {"zip_or_city": "30309"}, "capability": "plumbing"})\n  user: "Show me dentists in London"\n  -> call find_business({"vertical": "professional_services", "location": {"zip_or_city": "London"}, "capability": "dentist"})\n\nWHEN TO USE: Use when an agent needs to identify which SMBs can fulfill a business task (booking, service, consultation) in a given location and vertical. Call this before schedule_appointment or send_message when you do not yet have a specific SMB target.\nWHEN NOT TO USE: Do not use as a general directory or browsing surface. Do not use when you already have a specific verified SMB identifier. Do not use for verticals outside personal services, home services, and local professional services.\nCOST: from $0.01 per_call (see preview_cost for exact)\nLATENCY: ~200ms',
        inputSchema: {
          type: "object",
          required: [
            "vertical",
            "location"
          ],
          properties: {
            vertical: {
              type: "string",
              enum: [
                "personal_services",
                "home_services",
                "professional_services"
              ],
              description: "Service vertical to search within"
            },
            location: {
              type: "object",
              required: [
                "zip_or_city"
              ],
              properties: {
                zip_or_city: {
                  type: "string"
                },
                radius_miles: {
                  type: "number",
                  default: 10
                }
              }
            },
            capability: {
              type: "string",
              description: "Specific service capability required, e.g. 'haircut', 'plumbing', 'tax_consultation'"
            },
            price_band: {
              type: "object",
              properties: {
                max_usd: {
                  type: "number"
                }
              }
            },
            availability_window: {
              type: "object",
              properties: {
                start_iso: {
                  type: "string",
                  format: "date-time"
                },
                end_iso: {
                  type: "string",
                  format: "date-time"
                }
              }
            },
            max_results: {
              type: "integer",
              default: 5,
              maximum: 20
            }
          }
        },
        annotations: {
          title: "Find Business",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "verify_business",
        description: `Confirm that an SMB is real, currently operating, and capable of the requested service. Performs a live capability probe against the business's channel.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Confirm smb_imp_abc actually does emergency plumbing"
  -> call verify_business({"smb_id": "smb_imp_abc", "capability_to_verify": "emergency_plumbing"})

WHEN TO USE: Use before sending communications or scheduling if you have an unverified SMB identifier, or if the agent's task requires confirmed capability (e.g., 'I need to be sure they do emergency plumbing').
WHEN NOT TO USE: Do not use if the SMB was returned from find_business within the last 24 hours \u2014 those results are already verified.
COST: $0.02 per_call
LATENCY: ~500ms`,
        inputSchema: {
          type: "object",
          required: [
            "smb_id"
          ],
          properties: {
            smb_id: {
              type: "string"
            },
            capability_to_verify: {
              type: "string"
            }
          }
        },
        annotations: {
          title: "Verify Business",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "send_message",
        description: `Send a message on behalf of an agent's user or an SMB across SMS, email, or voice. Five message types: transactional, reminder, follow_up, notification, marketing. Every send routes through a non-bypassable compliance gate (TCPA, GDPR, CASL, PDPL across 22 jurisdictions) that enforces opt-in consent for marketing/promotional content \u2014 marketing without recorded consent is rejected at runtime with a structured compliance_violation receipt. Channel is abstracted: specify intent and recipient; the service selects and falls back across channels.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Text the salon I'll be 10 minutes late"
  -> call send_message({"recipient_id": "smb_xyz", "channel_preference": "sms", "message": {"body": "Will be 10 minutes late."}, "country_code": "US"})
  user: "Email the dentist about insurance"
  -> call send_message({"recipient_id": "smb_xyz", "channel_preference": "email", "message": {"body": "Do you accept Cigna?"}})

WHEN TO USE: Use to: (a) confirm a booking the agent just made, (b) reply to a customer who messaged the SMB first, (c) follow up on a quote the user requested, (d) send appointment reminders the SMB owes its customer, (e) send marketing messages to recipients who have opted in (with consent_record_id). The gate verifies consent on every send.
WHEN NOT TO USE: Do NOT use for OTPs or critical transactional confirmations \u2014 use send_transactional_confirmation. Do NOT attempt to send marketing without a consent_record_id pointing at a real opt-in \u2014 the gate will reject the send and log a compliance_violation. Do NOT attempt bulk / list-based / drip / cold outreach \u2014 those are out of scope and the rate limiter will throttle abuse.
COST: from $0.02 per_message (see preview_cost for exact)
LATENCY: ~800ms
EXECUTION: sync_fast (use get_outcome to retrieve result)`,
        inputSchema: {
          type: "object",
          required: [
            "recipient",
            "message_type",
            "content"
          ],
          properties: {
            recipient: {
              type: "object",
              required: [
                "id_type",
                "id_value"
              ],
              properties: {
                id_type: {
                  type: "string",
                  enum: [
                    "phone",
                    "email",
                    "smb_id",
                    "customer_id"
                  ]
                },
                id_value: {
                  type: "string"
                },
                country_code: {
                  type: "string",
                  description: "ISO 3166-1 alpha-2, required for compliance routing"
                }
              }
            },
            message_type: {
              type: "string",
              description: "Intent tag for the message. Five permitted types. 'marketing' is allowed only when paired with a valid consent_record_id; the compliance gate verifies the consent at send time and rejects (compliance_violation receipt) if it's missing, expired, or revoked.",
              enum: [
                "transactional",
                "marketing",
                "reminder",
                "follow_up",
                "notification"
              ]
            },
            content: {
              type: "object",
              required: [
                "body"
              ],
              properties: {
                body: {
                  type: "string"
                },
                subject: {
                  type: "string",
                  description: "For email channel"
                },
                template_id: {
                  type: "string"
                },
                template_vars: {
                  type: "object"
                }
              }
            },
            preferred_channel: {
              type: "string",
              enum: [
                "sms",
                "email",
                "voice",
                "auto"
              ],
              default: "auto"
            },
            send_at_iso: {
              type: "string",
              format: "date-time",
              description: "Schedule for future delivery; omit for immediate"
            }
          }
        },
        annotations: {
          title: "Send Message",
          readOnlyHint: false,
          destructiveHint: true,
          idempotentHint: false,
          openWorldHint: true
        }
      },
      {
        name: "capture_lead",
        description: `Structured intake of a prospect into an SMB's funnel with validation, enrichment hooks, and deduplication. Inserts into the SMB's CRM or direct-booking pipeline if available.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Tell smb_xyz I'm interested and want a callback"
  -> call capture_lead({"smb_id": "smb_xyz", "prospect": {"name": "Jane", "phone": "+15551234567", "email": "jane@example.com"}, "source": "agent"})

WHEN TO USE: Use when a potential customer has expressed interest in an SMB's service and you want to ensure they are registered in the SMB's pipeline for follow-up.
WHEN NOT TO USE: Do not use for confirmed bookings \u2014 use schedule_appointment. Do not use for bulk list imports.
COST: $0.05 per_lead
LATENCY: ~600ms
EXECUTION: sync_fast (use get_outcome to retrieve result)`,
        inputSchema: {
          type: "object",
          required: [
            "smb_id",
            "prospect"
          ],
          properties: {
            smb_id: {
              type: "string"
            },
            prospect: {
              type: "object",
              required: [
                "name"
              ],
              properties: {
                name: {
                  type: "string"
                },
                phone: {
                  type: "string"
                },
                email: {
                  type: "string",
                  format: "email"
                },
                service_interest: {
                  type: "string"
                },
                notes: {
                  type: "string"
                },
                consent_record_id: {
                  type: "string",
                  description: "Optional ID of a consent record proving the prospect asked to be contacted (e.g., they filled an SMB's intake form or requested a quote). Required when downstream send_message calls are anticipated."
                }
              }
            },
            source: {
              type: "string",
              description: "Where the consumer-initiated request originated (e.g., 'consumer_request', 'inbound_quote_form', 'agent_referral_from_find_business')."
            }
          }
        },
        annotations: {
          title: "Capture Lead",
          readOnlyHint: false,
          destructiveHint: false,
          idempotentHint: false,
          openWorldHint: false
        }
      },
      {
        name: "schedule_appointment",
        description: `Availability lookup, hold, confirm, reschedule, or cancel appointments with an SMB. Routes through the SMB's native booking system if available, falls back to voice AI or web form.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Book the haircut for next Tuesday at 3pm"
  -> call schedule_appointment({"smb_id": "smb_imp_abc", "action": "book", "service": "haircut"})
  user: "Cancel my Friday appointment at smb_xyz"
  -> call schedule_appointment({"smb_id": "smb_xyz", "action": "cancel"})
  user: "Reschedule my dental cleaning to next week"
  -> call schedule_appointment({"smb_id": "smb_imp_xyz", "action": "reschedule"})

WHEN TO USE: Use when an agent needs to book, reschedule, or cancel a specific appointment with a specific SMB. Requires a verified smb_id.
WHEN NOT TO USE: Do not use for bulk scheduling. Do not use without a verified SMB \u2014 call find_business and verify_business first if needed.
COST: from $0.15 per_booking_attempt (see preview_cost for exact)
LATENCY: ~5000ms
EXECUTION: async_by_default (use get_outcome to retrieve result)`,
        inputSchema: {
          type: "object",
          required: [
            "smb_id",
            "action"
          ],
          properties: {
            smb_id: {
              type: "string"
            },
            action: {
              type: "string",
              enum: [
                "book",
                "reschedule",
                "cancel",
                "check_availability"
              ]
            },
            service: {
              type: "string"
            },
            customer: {
              type: "object",
              properties: {
                name: {
                  type: "string"
                },
                phone: {
                  type: "string"
                },
                email: {
                  type: "string"
                }
              }
            },
            requested_time: {
              type: "object",
              properties: {
                preferred_iso: {
                  type: "string",
                  format: "date-time"
                },
                window_start_iso: {
                  type: "string",
                  format: "date-time"
                },
                window_end_iso: {
                  type: "string",
                  format: "date-time"
                },
                duration_minutes: {
                  type: "integer"
                }
              }
            },
            existing_appointment_id: {
              type: "string",
              description: "Required for reschedule/cancel"
            },
            notes: {
              type: "string"
            }
          }
        },
        annotations: {
          title: "Schedule Appointment",
          readOnlyHint: false,
          destructiveHint: true,
          idempotentHint: false,
          openWorldHint: true
        }
      },
      {
        name: "send_transactional_confirmation",
        description: 'Idempotent transactional messages: OTPs, booking confirmations, payment receipts, cancellation notices. Guaranteed delivery via redundant channels.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Send the booking confirmation receipt to my email"\n  -> call send_transactional_confirmation({"recipient_id": "user@example.com", "channel_preference": "email", "confirmation_type": "booking"})\n\nWHEN TO USE: Use for any message that MUST be delivered reliably \u2014 OTPs, booking confirmations, receipts. Do not use for marketing.\nWHEN NOT TO USE: Do not use for marketing or promotional messages. Do not use for conversational messages.\nCOST: $0.02 per_message\nLATENCY: ~500ms\nEXECUTION: sync_fast (use get_outcome to retrieve result)',
        inputSchema: {
          type: "object",
          required: [
            "recipient",
            "confirmation_type",
            "data"
          ],
          properties: {
            recipient: {
              type: "object",
              required: [
                "phone_or_email"
              ],
              properties: {
                phone_or_email: {
                  type: "string"
                },
                name: {
                  type: "string"
                }
              }
            },
            confirmation_type: {
              type: "string",
              enum: [
                "otp",
                "booking_confirmation",
                "payment_receipt",
                "cancellation_notice",
                "reminder"
              ]
            },
            data: {
              type: "object",
              description: "Type-specific payload; e.g., {otp_code} for otp, {appointment_time, smb_name} for booking_confirmation"
            },
            preferred_channel: {
              type: "string",
              enum: [
                "sms",
                "email",
                "auto"
              ],
              default: "sms"
            }
          }
        },
        annotations: {
          title: "Send Transactional Confirmation",
          readOnlyHint: false,
          destructiveHint: true,
          idempotentHint: false,
          openWorldHint: false
        }
      },
      {
        name: "handle_inbound",
        description: `Receive, classify, and route inbound messages on behalf of an SMB. Classifies intent (booking request, cancellation, inquiry, complaint), enriches with context, and routes to the appropriate handler or escalation path.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Process this customer reply for me: 'Yes I want to book Tuesday'"
  -> call handle_inbound({"raw_message": "Yes I want to book Tuesday", "channel": "sms"})

WHEN TO USE: Use when an SMB needs inbound message triage \u2014 classifying incoming contact-form submissions, SMS replies, voicemails, or email inquiries.
WHEN NOT TO USE: Do not use for outbound communications. Do not use for compliance-flagged recipient lists without verified opt-in records.
COST: $0.03 per_inbound
LATENCY: ~3000ms
EXECUTION: async_by_default (use get_outcome to retrieve result)`,
        inputSchema: {
          type: "object",
          required: [
            "smb_id",
            "inbound_channel",
            "raw_message"
          ],
          properties: {
            smb_id: {
              type: "string"
            },
            inbound_channel: {
              type: "string",
              enum: [
                "sms",
                "email",
                "voice_voicemail",
                "web_form",
                "api"
              ]
            },
            sender: {
              type: "object",
              properties: {
                phone: {
                  type: "string"
                },
                email: {
                  type: "string"
                },
                name: {
                  type: "string"
                }
              }
            },
            raw_message: {
              type: "string"
            },
            received_at_iso: {
              type: "string",
              format: "date-time"
            },
            routing_rules: {
              type: "object",
              description: "Optional override routing policy for this SMB"
            }
          }
        },
        annotations: {
          title: "Handle Inbound",
          readOnlyHint: false,
          destructiveHint: false,
          idempotentHint: false,
          openWorldHint: false
        }
      },
      {
        name: "escalate_to_human",
        description: `Hand off an in-flight task to a human operator with a full context bundle: transcript, prior actions, identifiers, and a recommended next step.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "I'm stuck \u2014 get a human at smb_xyz to call me back"
  -> call escalate_to_human({"smb_id": "smb_xyz", "reason": "agent_blocked", "summary": "Cannot resolve via automated channels"})

WHEN TO USE: Use when automated resolution has failed after channel-fallback exhaustion, when the task requires human judgment, or when the customer has explicitly requested human contact.
WHEN NOT TO USE: Do not use as a first resort. Escalate only after automated resolution attempts.
COST: $0.2 per_escalation
LATENCY: ~2000ms
EXECUTION: async_by_default (use get_outcome to retrieve result)`,
        inputSchema: {
          type: "object",
          required: [
            "smb_id",
            "reason",
            "context"
          ],
          properties: {
            smb_id: {
              type: "string"
            },
            reason: {
              type: "string",
              enum: [
                "automation_failed",
                "customer_requested",
                "compliance_hold",
                "ambiguous_intent",
                "exception_required"
              ]
            },
            context: {
              type: "object",
              properties: {
                original_operation: {
                  type: "string"
                },
                operation_id: {
                  type: "string"
                },
                transcript: {
                  type: "array",
                  items: {
                    type: "object"
                  }
                },
                prior_actions: {
                  type: "array",
                  items: {
                    type: "object"
                  }
                },
                recommended_next_step: {
                  type: "string"
                }
              }
            },
            priority: {
              type: "string",
              enum: [
                "normal",
                "urgent"
              ],
              default: "normal"
            }
          }
        },
        annotations: {
          title: "Escalate To Human",
          readOnlyHint: false,
          destructiveHint: true,
          idempotentHint: false,
          openWorldHint: false
        }
      },
      {
        name: "get_status",
        description: "Query the current state of any in-flight async operation by operation_id.\n\nWHEN TO USE: Use to poll the state of a pending_async operation when no webhook callback has arrived or to check progress.\nWHEN NOT TO USE: Do not poll more frequently than once per 10 seconds \u2014 use webhook delivery for real-time updates instead.\nCOST: $0.001 per_call\nLATENCY: ~50ms",
        inputSchema: {
          type: "object",
          required: [
            "operation_id"
          ],
          properties: {
            operation_id: {
              type: "string"
            }
          }
        },
        annotations: {
          title: "Get Status",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "get_outcome",
        description: "Retrieve the final OutcomeReceipt for a completed operation.\n\nWHEN TO USE: Use after get_status returns success/failure/partial to retrieve the full result with cost and reason codes.\nWHEN NOT TO USE: Do not use for operations still in pending/executing state \u2014 use get_status first.\nCOST: $0.001 per_call\nLATENCY: ~50ms",
        inputSchema: {
          type: "object",
          required: [
            "operation_id"
          ],
          properties: {
            operation_id: {
              type: "string"
            }
          }
        },
        annotations: {
          title: "Get Outcome",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "preview_cost",
        description: 'Return an expected cost estimate, latency estimate, and success-probability estimate for a proposed call before execution. Accuracy SLO: actual cost within \xB15% of preview.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "How much will this SMS cost me?"\n  -> call preview_cost({"operation": "send_message", "params": {"channel_preference": "sms"}})\n  user: "Estimate the cost of booking via voice fallback"\n  -> call preview_cost({"operation": "schedule_appointment"})\n\nWHEN TO USE: Use before any operation when the agent is operating under a budget constraint and needs to decide whether to proceed.\nWHEN NOT TO USE: Do not use in a hot loop \u2014 cache the result for at least 60 seconds if repeating the same preview.\nCOST: $0.001 per_call\nLATENCY: ~100ms',
        inputSchema: {
          type: "object",
          required: [
            "operation",
            "params"
          ],
          properties: {
            operation: {
              type: "string"
            },
            params: {
              type: "object",
              description: "The same request body you would pass to the operation"
            }
          }
        },
        annotations: {
          title: "Preview Cost",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "self_test",
        description: 'Live capability probe that verifies the service is healthy, each claimed operation is reachable, and supply network size is current. Use to verify integration before production use.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Run a health check before I send the broadcast"\n  -> call self_test({})\n\nWHEN TO USE: Use at agent startup, before high-stakes task sequences, or after receiving unexpected errors to check if the service is degraded.\nWHEN NOT TO USE: Do not call more than once per minute in production.\nCOST: free\nLATENCY: ~200ms',
        inputSchema: {
          type: "object",
          properties: {}
        },
        annotations: {
          title: "Self Test",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "check_booking_link",
        description: 'Free, instant pre-flight check for a booking URL. Classifies which booking platform a URL belongs to and tells you whether import_booking_url will accept it, WITHOUT fetching the page or spending money. Returns the platform, the exact smb_id import_booking_url would assign, the channels the booking will route through, and the inferred country. Use it to de-risk a paid booking BEFORE calling import_booking_url + schedule_appointment.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Is this a bookable link? https://cal.com/jane"\n  -> call check_booking_link({"url": "https://cal.com/jane"})\n  -> then import_booking_url({"booking_url": "https://cal.com/jane"})\n  user: "Can you book me here: https://www.opentable.com/r/acme-bistro"\n  -> call check_booking_link({"url": "https://www.opentable.com/r/acme-bistro"})\n\nWHEN TO USE: Call this the moment a user pastes a URL and you are not sure it is a bookable page, or before you commit to a paid schedule_appointment. It is free and sub-100ms, so run it as a guard: if supported=true, proceed to import_booking_url with confidence; if supported=false, fall back to find_business or call_business instead of wasting a booking attempt.\nWHEN NOT TO USE: Do not use to confirm the page is currently live/available \u2014 this tool does not fetch the URL, it only classifies its shape. It is not a substitute for import_booking_url (which actually registers the business) or verify_business (which confirms an already-imported smb_id).\nCOST: free\nLATENCY: ~20ms',
        inputSchema: {
          type: "object",
          required: [
            "url"
          ],
          properties: {
            url: {
              type: "string",
              description: "Full http(s) URL to classify, e.g. 'https://cal.com/jane' or 'https://www.opentable.com/r/acme'."
            }
          }
        },
        annotations: {
          title: "Check Booking Link",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "import_booking_url",
        description: `Turn ANY public booking URL (Cal.com, Calendly, Doctolib, Booksy, Fresha, OpenTable, Setmore, Square, Acuity, Schedulista, Squarespace, BookMyCity) into a callable smb_id you can immediately use with schedule_appointment, send_message, or capture_lead. Idempotent \u2014 calling twice returns the same smb_id.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Book me a haircut at https://cal.com/jane-salon"
  -> call import_booking_url({"booking_url": "https://cal.com/jane-salon", "vertical": "personal_services"})
  -> then schedule_appointment({"smb_id": "<from_above>", "action": "book"})
  user: "Schedule with this dentist: https://www.doctolib.fr/dentiste/paris/jean-dupont"
  -> call import_booking_url({"booking_url": "https://www.doctolib.fr/dentiste/paris/jean-dupont"})
  user: "Reserve a table at https://www.opentable.com/r/acme-bistro"
  -> call import_booking_url({"booking_url": "https://www.opentable.com/r/acme-bistro", "vertical": "restaurants"})

WHEN TO USE: Call this FIRST whenever the user provides a specific booking URL (cal.com/handle, calendly.com/handle/event, doctolib.fr/..., booksy.com/..., opentable.com/r/..., etc.). User patterns that match: 'book me at https://cal.com/...', 'schedule with calendly.com/jane/intro', 'reserve a table at opentable.com/r/...', 'I want to book this dentist: https://www.doctolib.fr/...'. After importing, the returned smb_id can be passed straight to schedule_appointment.
WHEN NOT TO USE: Do not use if the user only describes a business by name without a URL \u2014 call find_business instead. Do not use for arbitrary websites that are not on the supported booking-platform list (use /supply/platforms to see all 12).
COST: $0.005 per_call
LATENCY: ~600ms`,
        inputSchema: {
          type: "object",
          required: [
            "booking_url"
          ],
          properties: {
            booking_url: {
              type: "string",
              format: "uri",
              description: "Full URL the user supplied. Must point at one of the 12 supported booking platforms; auto-detected from the host."
            },
            business_name: {
              type: "string",
              description: "Optional override. If omitted, the business name is auto-extracted from the page's <title> or og:title."
            },
            vertical: {
              type: "string",
              enum: [
                "personal_services",
                "home_services",
                "professional_services",
                "restaurants",
                "retail",
                "healthcare",
                "fitness"
              ],
              description: "Best-guess vertical. If omitted, inferred from the platform (e.g., Doctolib -> healthcare, OpenTable -> restaurants)."
            },
            country_code: {
              type: "string",
              description: "ISO 3166-1 alpha-2 (e.g. 'US', 'FR'). Used for compliance routing on later send_message calls."
            },
            contact_phone: {
              type: "string",
              description: "Optional. If omitted, the platform integration handles outreach."
            },
            contact_email: {
              type: "string",
              description: "Optional."
            },
            capabilities: {
              type: "array",
              items: {
                type: "string"
              },
              description: "Free-form capability tags (e.g., ['haircut','color','blowdry'])."
            }
          }
        },
        annotations: {
          title: "Import Booking Url",
          readOnlyHint: false,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "call_business",
        description: "Place a conversational voice-AI phone call to a business on a consumer's behalf and return a structured answer. THE differentiated capability: reach the ~60M long-tail SMBs that have NO API and NO booking page \u2014 only a phone number. An AI agent cannot pick up a phone and hold a conversation; this tool does. Give a plain-language objective; the voice AI navigates the call and extracts the answer. Business-directed (B2B), far less restricted than calling consumers \u2014 but the compliance gate still enforces recording consent per jurisdiction. Async: returns a call handle; poll get_outcome for the transcript + extracted fields.\n\nWHEN TO USE: Use when the target business has NO booking URL and NO API \u2014 only a phone number \u2014 and the consumer asked the agent to reach them (e.g. 'call this plumber and ask if they can come Tuesday', 'ask the salon if they take walk-ins this afternoon'). Also use to confirm details a booking page doesn't expose (real-time availability, custom quotes).\nWHEN NOT TO USE: Do NOT use when the business has a booking URL \u2014 use import_booking_url + schedule_appointment (cheaper, faster, deterministic). Do NOT use for calls to consumers/individuals (this tool is for reaching businesses). Do NOT use for marketing or telemarketing \u2014 the compliance gate and the B2B-only framing reject that.\nCOST: $0.5 per_call\nLATENCY: ~45000ms\nEXECUTION: async_by_default (use get_outcome to retrieve result)",
        inputSchema: {
          type: "object",
          required: [
            "objective"
          ],
          properties: {
            business_phone: {
              type: "string",
              description: "Business phone in E.164 (e.g. +14045550123). Provide this OR smb_id."
            },
            smb_id: {
              type: "string",
              description: "Known SMB identifier with a phone on record. Provide this OR business_phone."
            },
            objective: {
              type: "string",
              description: "What the call should accomplish, in plain language."
            },
            extract_fields: {
              type: "array",
              items: {
                type: "string"
              },
              description: "Structured fields to pull from the answer, e.g. ['available_tomorrow','price_quote','earliest_slot']."
            },
            country_code: {
              type: "string",
              description: "ISO 3166-1 alpha-2 for compliance + recording-consent routing."
            },
            on_behalf_of: {
              type: "string",
              description: "Name of the consumer the call is placed for."
            },
            max_duration_seconds: {
              type: "integer",
              maximum: 600,
              default: 180
            }
          }
        },
        annotations: {
          title: "Call Business",
          readOnlyHint: false,
          destructiveHint: true,
          idempotentHint: false,
          openWorldHint: true
        }
      },
      {
        name: "check_compliance",
        description: `Free, instant pre-flight for the compliance gate. Runs the SAME TCPA / GDPR / CASL / CAN-SPAM / 10DLC gate that send_message and call_business run \u2014 but in preview mode, so NO message is sent and NO state changes. Tells you whether a (recipient, channel, message_type, content) send would be permitted BEFORE you pay for it, and if not, names the exact rule and how to remediate. Use it to de-risk a paid send the same way check_booking_link de-risks a paid booking.

EXAMPLE USER QUERIES THAT MATCH THIS TOOL:
  user: "Is it legal to text this US number a 20%-off promo?"
  -> call check_compliance({"recipient_id": "+14045550200", "content": "20% off this week only!", "channel": "sms", "message_type": "marketing", "country_code": "US"})
  user: "Before you email the dentist, make sure it's allowed"
  -> call check_compliance({"recipient_id": "office@dentist.example", "content": "Do you accept Cigna? Following up on my request.", "message_type": "follow_up"})
  -> then send_message({"recipient": {"id_type": "email", "id_value": "office@dentist.example"}, "message_type": "follow_up", "content": {"body": "Do you accept Cigna? Following up on my request."}})

WHEN TO USE: Call this the moment before send_message or call_business when there is any chance the send is regulated \u2014 anything tagged marketing, any SMS to a US number (10DLC), any message to an EU/UK (GDPR) or Canadian (CASL) recipient, or any content you are unsure about. It is free and sub-100ms, so run it as a guard: if legal=true, proceed to send_message with confidence; if legal=false, fix the cited blocker instead of burning a paid, rejected send.
WHEN NOT TO USE: Do not treat a legal=true as a permanent license \u2014 the gate re-runs at send time, so a fresh opt-out between preview and send still blocks. Do not use it to check two-party voice recording consent (that is evaluated at call time in the voice adapter, not here). It is not a substitute for send_message; it never delivers anything.
COST: free
LATENCY: ~15ms`,
        inputSchema: {
          type: "object",
          required: [
            "recipient_id",
            "content"
          ],
          properties: {
            recipient_id: {
              type: "string",
              description: "Phone in E.164 (e.g. '+14045550100') or email address the message would go to."
            },
            content: {
              type: "string",
              description: "The actual message body you intend to send. The gate classifies the real text, so a meaningful preview needs the real content."
            },
            channel: {
              type: "string",
              enum: [
                "sms",
                "email",
                "voice"
              ],
              description: "Delivery channel. Omit to auto-infer sms/email from recipient_id; set 'voice' explicitly."
            },
            message_type: {
              type: "string",
              description: "Intent tag: transactional, marketing, reminder, follow_up, notification. 'marketing' triggers the consent checks. Defaults to transactional.",
              default: "transactional"
            },
            country_code: {
              type: "string",
              description: "ISO 3166-1 alpha-2 (e.g. 'US', 'DE', 'CA'). Auto-inferred from phone if omitted; drives which jurisdiction rules apply."
            },
            state_code: {
              type: "string",
              description: "US state code (e.g. 'CA') for state-specific rules."
            }
          }
        },
        annotations: {
          title: "Check Compliance",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "verify_company_record",
        description: 'Free, live lookup of a company official registry record. Queries the GLEIF global LEI registry (primary, 2.6 million legal entities worldwide) and SEC EDGAR (US public companies) to return the official legal name, LEI, entity status, jurisdiction, registered address, and registry authority. Never fabricates: if the company is not found in these free registries, returns an honest not_found with the sources that were queried.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Is Apple Inc a real registered company?"\n  -> call verify_company_record({"name": "Apple Inc", "country": "US"})\n  user: "Look up the LEI for Volkswagen AG"\n  -> call verify_company_record({"name": "Volkswagen AG", "country": "DE"})\n  user: "Verify this LEI: 529900HNOAA1KXQJUQ27"\n  -> call verify_company_record({"name": "Volkswagen AG", "lei": "529900HNOAA1KXQJUQ27"})\n\nWHEN TO USE: Use when you need to verify that a company exists as a registered legal entity and retrieve its official registry details -- before signing a contract, qualifying a vendor, validating a counterparty, or populating a due-diligence record. Accepts a legal name plus optional country filter or a direct LEI for a precise lookup.\nWHEN NOT TO USE: Do not use to verify private companies not registered with GLEIF or SEC. Do not use as an exhaustive fraud-detection tool; this is a first-pass existence check against free public registries, not a full KYC screen.\nCOST: free\nLATENCY: ~800ms',
        inputSchema: {
          type: "object",
          required: [
            "name"
          ],
          properties: {
            name: {
              type: "string",
              description: "Legal company name to look up, e.g. Apple Inc or Volkswagen AG."
            },
            country: {
              type: "string",
              description: "Optional ISO 3166-1 alpha-2 country filter (e.g. US, DE, GB). Narrows GLEIF results to one jurisdiction."
            },
            lei: {
              type: "string",
              description: "Optional 20-character Legal Entity Identifier for a direct, precise lookup."
            }
          }
        },
        annotations: {
          title: "Verify Company Record",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "screen_sanctions",
        description: 'Free, live screening of a name or entity against official sanctions and watchlists. Queries OpenSanctions (aggregates OFAC SDN, EU Consolidated Financial Sanctions, UN Security Council, UK HMT, and 40+ official lists) plus the OFAC SDN list directly from the US Treasury. Returns matched: bool, a list of matches with score, program, and source URL, and which lists were screened. Never fabricates a match or a clear -- if no match is found, explicitly names which lists were checked.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Screen this vendor before we pay them: ACME Trading LLC, Russia"\n  -> call screen_sanctions({"name": "ACME Trading LLC", "country": "RU", "type": "entity"})\n  user: "Is Kim Jong-un on the OFAC list?"\n  -> call screen_sanctions({"name": "Kim Jong-un", "country": "KP", "type": "person"})\n  user: "Run a sanctions check on this person before onboarding"\n  -> call screen_sanctions({"name": "Ivan Petrov", "country": "RU", "type": "person"})\n  user: "Do a compliance check -- is this company sanctioned?"\n  -> call screen_sanctions({"name": "Mahan Air", "country": "IR", "type": "entity"})\n\nWHEN TO USE: Use before onboarding a counterparty, processing a payment, engaging a vendor, or doing any due-diligence step that requires knowing whether a person or entity appears on official sanctions lists. Essential for agents doing business formation, vendor qualification, payments onboarding, trade compliance, or any workflow where a sanctioned counterparty is a legal or reputational risk.\nWHEN NOT TO USE: Do not use as a substitute for full KYC/AML screening -- this covers sanctions lists only, not PEP (Politically Exposed Person) databases, adverse media, or credit risk. Do not treat a negative result as a compliance clearance; it is informational only. Do not use for bulk screening of large lists -- each call is a live API query.\nCOST: free\nLATENCY: ~2000ms',
        inputSchema: {
          type: "object",
          required: [
            "name"
          ],
          properties: {
            name: {
              type: "string",
              description: "Full name of the person or entity to screen, e.g. 'Kim Jong-un' or 'ACME Trading LLC'. Use the most complete name available for best accuracy."
            },
            country: {
              type: "string",
              description: "Optional ISO 3166-1 alpha-2 country code (e.g. 'US', 'RU', 'IR'). Narrows results to entities associated with this country."
            },
            type: {
              type: "string",
              enum: [
                "person",
                "entity"
              ],
              description: "Optional entity type hint. 'person' for individuals, 'entity' for organizations/companies. Omit to screen both."
            }
          }
        },
        annotations: {
          title: "Screen Sanctions",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      },
      {
        name: "map_trade_restriction",
        description: 'Free, live cross-border trade-compliance snapshot. Given a product and destination country (and optionally an HS code, origin country, and a list of parties to screen), returns: (a) whether the destination or any party hits an export-control or sanctions restriction, (b) the destination risk level (comprehensive_embargo / sectoral_sanctions / elevated_scrutiny / standard), (c) HS code hint if the caller provided one, (d) honest tariff guidance + official links without fabricated rates, and (e) party sanctions screening via OpenSanctions and OFAC SDN. Acts as a MIDDLEMAN -- unifies the OFAC comprehensive-embargo map, OpenSanctions (40+ official lists including BIS Entity List, EU, UN, UK), and OFAC SDN into one clean call. Never fabricates a tariff rate, a clear, or a restricted status.\n\nEXAMPLE USER QUERIES THAT MATCH THIS TOOL:\n  user: "Can we ship laptops to Iran?"\n  -> call map_trade_restriction({"product": "laptop computers", "destination_country": "IR"})\n  user: "Screen this supplier before we import from them: Mahan Air, Iran"\n  -> call map_trade_restriction({"product": "aircraft parts", "destination_country": "US", "parties": ["Mahan Air"]})\n  user: "Is exporting hydraulic pumps to Russia restricted?"\n  -> call map_trade_restriction({"product": "hydraulic pumps", "hs_code": "8413.50", "destination_country": "RU"})\n  user: "Check if we can sell medical devices to Germany, supplier is ACME GmbH"\n  -> call map_trade_restriction({"product": "medical devices", "origin_country": "US", "destination_country": "DE", "parties": ["ACME GmbH"]})\n\nWHEN TO USE: Use before any cross-border trade to flag embargoed destinations, screen exporters/importers/freight forwarders against sanctions lists, and get authoritative links to the applicable tariff databases. Call this as a pre-flight check before quoting, invoicing, or shipping internationally. Covers OFAC comprehensively-embargoed countries (Iran, North Korea, Cuba, Syria) and significant advisory countries (Russia, Belarus, Ukraine Crimea/DNR/LNR regions).\nWHEN NOT TO USE: Do NOT use as a substitute for a licensed export compliance review. Do NOT use to obtain authoritative tariff rates (this tool returns guidance links, never fabricated rates). Do NOT use for purely domestic shipments where no cross-border movement is involved.\nCOST: free\nLATENCY: ~3000ms',
        inputSchema: {
          type: "object",
          required: [
            "product",
            "destination_country"
          ],
          properties: {
            product: {
              type: "string",
              description: "Product name or description, e.g. 'laptop computers', 'crude oil', 'medical devices'. Used in the tariff guidance note."
            },
            hs_code: {
              type: "string",
              description: "Optional Harmonized System code (e.g. '8471.30' for laptops). If provided, echoed back and included in tariff guidance. Not derived -- caller must supply the official HS code."
            },
            origin_country: {
              type: "string",
              description: "Optional ISO 3166-1 alpha-2 code for the exporting country (e.g. 'US', 'DE'). Used in the tariff guidance note."
            },
            destination_country: {
              type: "string",
              description: "ISO 3166-1 alpha-2 code for the importing country (e.g. 'IR', 'CA', 'DE'). Required. Checked against the OFAC comprehensive-embargo map and sectoral-sanctions advisory list."
            },
            parties: {
              type: "array",
              items: {
                type: "string"
              },
              description: "Optional list of party names to screen (exporter, importer, freight forwarder, end-user, etc.). Each name is screened against OpenSanctions (40+ official lists) and OFAC SDN."
            }
          }
        },
        annotations: {
          title: "Map Trade Restriction",
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false
        }
      }
    ]
  }
};

// src/snapshots/mcp-initialize.json
var mcp_initialize_default = { jsonrpc: "2.0", id: 1, result: { protocolVersion: "2024-11-05", serverInfo: { name: "smb-broker", version: "0.1.0" }, capabilities: { tools: { listChanged: false }, resources: { listChanged: false, subscribe: false }, prompts: { listChanged: false }, logging: {} }, instructions: "SMB Transaction & Communication Broker. Use tools/list to see all 17 operations. Most operations require an X-Agent-Identity token in the underlying HTTP request. For state-changing operations (send_message, schedule_appointment), call preview_cost first to confirm the budget impact." } };

// src/snapshots/index.ts
import llmsTxtRaw from "./a5115d1df106ae2c122c34ebb0eea763c03dfc9e-llms.txt";
import llmsFullTxtRaw from "./3f5473a4dfaa43d3a259f69af977ccef04c5a305-llms-full.txt";
import openapiYamlRaw from "./fa66d007416b9583c8bb32104bd0da1a2912fcb2-openapi.yaml";
var URL_PATTERNS = [
  "https://agentbroker.qzz.io",
  "https://smb-broker.onrender.com",
  "https://api.smb-broker.example/v1",
  "https://api.smb-broker.example"
];
function rewriteUrlsInString(s, replacement) {
  let out = s;
  for (const orig of URL_PATTERNS) {
    out = out.split(orig).join(replacement);
  }
  return out;
}
__name(rewriteUrlsInString, "rewriteUrlsInString");
function rewriteUrls(input, replacement) {
  if (typeof input === "string") {
    return rewriteUrlsInString(input, replacement);
  }
  if (Array.isArray(input)) {
    return input.map((x) => rewriteUrls(x, replacement));
  }
  if (input !== null && typeof input === "object") {
    const result = {};
    for (const k of Object.keys(input)) {
      result[k] = rewriteUrls(input[k], replacement);
    }
    return result;
  }
  return input;
}
__name(rewriteUrls, "rewriteUrls");
var cached = null;
function getSnapshots(publicBaseUrl) {
  if (cached && cached.url === publicBaseUrl) return cached.data;
  const data = {
    manifest: rewriteUrls(manifest_default, publicBaseUrl),
    agentService: rewriteUrls(agent_service_default, publicBaseUrl),
    agentsJson: rewriteUrls(agents_default, publicBaseUrl),
    aiPluginJson: rewriteUrls(ai_plugin_default, publicBaseUrl),
    openaiToolsJson: rewriteUrls(openai_tools_default, publicBaseUrl),
    anthropicToolsJson: rewriteUrls(anthropic_tools_default, publicBaseUrl),
    mcpJson: rewriteUrls(mcp_default, publicBaseUrl),
    supplyPlatforms: supply_platforms_default,
    jurisdictions: jurisdictions_default,
    mcpToolsList: mcp_tools_list_default,
    mcpInitialize: mcp_initialize_default,
    llmsTxt: rewriteUrlsInString(llmsTxtRaw, publicBaseUrl),
    llmsFullTxt: rewriteUrlsInString(llmsFullTxtRaw, publicBaseUrl),
    openapiYaml: rewriteUrlsInString(openapiYamlRaw, publicBaseUrl)
  };
  cached = { url: publicBaseUrl, data };
  return data;
}
__name(getSnapshots, "getSnapshots");
function manifestVersion(snapshots) {
  const m = snapshots.manifest;
  return {
    version: m.service?.version ?? "0.1.0",
    last_updated: "2026-05-05T05:00:00Z",
    service_name: "smb-broker",
    operation_count: Array.isArray(m.operations) ? m.operations.length : 13
  };
}
__name(manifestVersion, "manifestVersion");
function manifestOps(snapshots) {
  const m = snapshots.manifest;
  const ops = m.operations ?? [];
  return {
    operations: ops.map((op) => ({
      name: op.name,
      description: op.description,
      when_to_use: op.when_to_use,
      execution_profile: op.execution_profile,
      cost_model: op.cost_model,
      slo: op.slo
    }))
  };
}
__name(manifestOps, "manifestOps");

// src/discovery.ts
var JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*"
};
var TEXT_HEADERS = {
  "content-type": "text/plain; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*"
};
var YAML_HEADERS = {
  "content-type": "application/x-yaml; charset=utf-8",
  "cache-control": "public, max-age=300",
  "access-control-allow-origin": "*"
};
function jsonRes(staticValue, live) {
  const body = JSON.stringify(live ?? staticValue);
  return new Response(body, {
    status: 200,
    headers: {
      ...JSON_HEADERS,
      "x-edge-source": live !== null ? "kv-live" : "embedded"
    }
  });
}
__name(jsonRes, "jsonRes");
function textRes(staticValue, live, contentType) {
  return new Response(live ?? staticValue, {
    status: 200,
    headers: {
      ...contentType,
      "x-edge-source": live !== null ? "kv-live" : "embedded"
    }
  });
}
__name(textRes, "textRes");
var DISCOVERY_HANDLERS = {
  "/.well-known/agent-service": /* @__PURE__ */ __name((s, kv) => jsonRes(s.agentService, kv), "/.well-known/agent-service"),
  "/.well-known/agents.json": /* @__PURE__ */ __name((s, kv) => jsonRes(s.agentsJson, kv), "/.well-known/agents.json"),
  "/.well-known/ai-plugin.json": /* @__PURE__ */ __name((s, kv) => jsonRes(s.aiPluginJson, kv), "/.well-known/ai-plugin.json"),
  "/.well-known/openai-tools.json": /* @__PURE__ */ __name((s, kv) => jsonRes(s.openaiToolsJson, kv), "/.well-known/openai-tools.json"),
  "/.well-known/anthropic-tools.json": /* @__PURE__ */ __name((s, kv) => jsonRes(s.anthropicToolsJson, kv), "/.well-known/anthropic-tools.json"),
  "/.well-known/mcp.json": /* @__PURE__ */ __name((s, kv) => jsonRes(s.mcpJson, kv), "/.well-known/mcp.json"),
  "/manifest": /* @__PURE__ */ __name((s, kv) => jsonRes(s.manifest, kv), "/manifest"),
  "/manifest/version": /* @__PURE__ */ __name((s, kv) => jsonRes(manifestVersion(s), kv), "/manifest/version"),
  "/manifest/ops": /* @__PURE__ */ __name((s, kv) => jsonRes(manifestOps(s), kv), "/manifest/ops"),
  "/supply/platforms": /* @__PURE__ */ __name((s, kv) => jsonRes(s.supplyPlatforms, kv), "/supply/platforms"),
  "/compliance/jurisdictions": /* @__PURE__ */ __name((s, kv) => jsonRes(s.jurisdictions, kv), "/compliance/jurisdictions"),
  "/llms.txt": /* @__PURE__ */ __name((s, kv) => textRes(s.llmsTxt, kv, TEXT_HEADERS), "/llms.txt"),
  "/llms-full.txt": /* @__PURE__ */ __name((s, kv) => textRes(s.llmsFullTxt, kv, TEXT_HEADERS), "/llms-full.txt"),
  "/openapi.yaml": /* @__PURE__ */ __name((s, kv) => textRes(s.openapiYaml, kv, YAML_HEADERS), "/openapi.yaml")
};
function kvKey(publicBaseUrl, path) {
  return `live:v1:${publicBaseUrl}:${path}`;
}
__name(kvKey, "kvKey");
async function tryServeDiscovery(request, publicBaseUrl, kv) {
  if (request.method !== "GET" && request.method !== "HEAD") return null;
  const url = new URL(request.url);
  const handler = DISCOVERY_HANDLERS[url.pathname];
  if (!handler) return null;
  const snapshots = getSnapshots(publicBaseUrl);
  const isJson = url.pathname.endsWith(".json") || url.pathname === "/manifest" || url.pathname === "/manifest/version" || url.pathname === "/manifest/ops" || url.pathname === "/supply/platforms" || url.pathname === "/compliance/jurisdictions";
  let live = null;
  try {
    live = isJson ? await kv.get(kvKey(publicBaseUrl, url.pathname), "json") : await kv.get(kvKey(publicBaseUrl, url.pathname), "text");
  } catch {
    live = null;
  }
  const resp = handler(snapshots, live);
  if (request.method === "HEAD") {
    return new Response(null, { status: resp.status, headers: resp.headers });
  }
  return resp;
}
__name(tryServeDiscovery, "tryServeDiscovery");
async function refreshKvFromOrigin(kv, originUrl, publicBaseUrl, pathname, isJson) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 1e4);
    const r = await fetch(originUrl + pathname, {
      headers: { "x-edge-probe": "cron-refresh" },
      signal: ctrl.signal
    });
    clearTimeout(t);
    if (!r.ok) return;
    const body = await r.text();
    let rewritten = body;
    for (const orig of [
      "https://agent-broker-edge.basil-agent.workers.dev",
      "https://smb-broker.onrender.com",
      "https://api.smb-broker.example/v1",
      "https://api.smb-broker.example"
    ]) {
      rewritten = rewritten.split(orig).join(publicBaseUrl);
    }
    if (isJson) {
      try {
        JSON.parse(rewritten);
      } catch {
        return;
      }
    }
    await kv.put(kvKey(publicBaseUrl, pathname), rewritten, {
      expirationTtl: 3600
      // 1 hour — cron refreshes every 2 min anyway
    });
  } catch {
  }
}
__name(refreshKvFromOrigin, "refreshKvFromOrigin");

// src/x402.ts
var USDC_BASE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
var BASE_RPC_URL = "https://mainnet.base.org";
var ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
var PROOF_MAX_AGE_SEC = 600;
var PRICING_ATOMIC = {
  // Free reads — evaluation tier, never gated. Catalog scorers happy.
  find_business: 0,
  verify_business: 0,
  get_status: 0,
  get_outcome: 0,
  preview_cost: 0,
  self_test: 0,
  // Paid writes — real cost-to-serve, x402-gated when receiver is configured.
  // Halved from the original spec to be agent-friendly. Volume > margin.
  send_message: 2e4,
  // $0.02 (was $0.05) — Twilio SMS cost ~$0.0075
  capture_lead: 5e4,
  // $0.05 (was $0.10)
  schedule_appointment: 15e4,
  // $0.15 (was $0.25) — Cal.com is free; voice fallback is the only paid path
  send_transactional_confirmation: 2e4,
  // $0.02 (was $0.03)
  handle_inbound: 3e4,
  // $0.03 (was $0.08) — mostly LLM classification, ~$0.001 cost
  escalate_to_human: 2e5,
  // $0.20 (was $0.50) — most escalations are routing, not human-touch
  import_booking_url: 5e3,
  // $0.005 (unchanged — already cheap)
  call_business: 5e5
  // $0.50 — conversational Vapi call (~$0.10-0.40 cost/2-3min) + margin
};
function getRequiredAmount(toolName) {
  return PRICING_ATOMIC[toolName] ?? 0;
}
__name(getRequiredAmount, "getRequiredAmount");
function isPricedTool(toolName) {
  return (PRICING_ATOMIC[toolName] ?? 0) > 0;
}
__name(isPricedTool, "isPricedTool");
function generateNonce() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}
__name(generateNonce, "generateNonce");
function buildPaymentRequirements(toolName, recipient) {
  const amount = getRequiredAmount(toolName);
  const nonce = generateNonce();
  const requirements = {
    scheme: "exact-evm",
    chain: "base",
    network: "base-mainnet",
    recipient,
    amount_atomic: String(amount),
    currency: "USDC",
    currency_address: USDC_BASE_ADDRESS,
    nonce,
    expires_at_unix: Math.floor(Date.now() / 1e3) + PROOF_MAX_AGE_SEC
  };
  return { requirements, nonce };
}
__name(buildPaymentRequirements, "buildPaymentRequirements");
async function rpc(method, params) {
  try {
    const res = await fetch(BASE_RPC_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params })
    });
    if (!res.ok) return null;
    const json = await res.json();
    if (json.error) return null;
    return json.result ?? null;
  } catch {
    return null;
  }
}
__name(rpc, "rpc");
function hexToBigInt(hex) {
  return BigInt(hex);
}
__name(hexToBigInt, "hexToBigInt");
function normalizeAddr(addr) {
  return addr.toLowerCase();
}
__name(normalizeAddr, "normalizeAddr");
function topicToAddress(topic) {
  if (!topic.startsWith("0x") || topic.length !== 66) return "";
  return "0x" + topic.slice(26).toLowerCase();
}
__name(topicToAddress, "topicToAddress");
async function verifyPaymentOnchain(txHash, expectedRecipient, expectedAmountAtomic, kvNonceKey, kv) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(txHash)) {
    return { valid: false, reason: "tx_hash_malformed" };
  }
  const nonceState = await kv.get(kvNonceKey);
  if (nonceState === null) {
    return { valid: false, reason: "nonce_unknown_or_expired" };
  }
  if (nonceState === "spent") {
    return { valid: false, reason: "nonce_already_spent" };
  }
  const tx = await rpc("eth_getTransactionByHash", [txHash]);
  if (!tx) return { valid: false, reason: "tx_not_found" };
  if (!tx.blockNumber) return { valid: false, reason: "tx_not_mined" };
  const receipt = await rpc("eth_getTransactionReceipt", [txHash]);
  if (!receipt) return { valid: false, reason: "receipt_not_found" };
  if (receipt.status !== "0x1") return { valid: false, reason: "tx_reverted" };
  if (normalizeAddr(receipt.to) !== normalizeAddr(USDC_BASE_ADDRESS)) {
    return { valid: false, reason: "tx_target_not_usdc" };
  }
  const expectedRecipientLc = normalizeAddr(expectedRecipient);
  let matched = false;
  for (const log of receipt.logs ?? []) {
    if (normalizeAddr(log.address) !== normalizeAddr(USDC_BASE_ADDRESS)) continue;
    if (!log.topics || log.topics.length < 3) continue;
    if (log.topics[0].toLowerCase() !== ERC20_TRANSFER_TOPIC) continue;
    const toAddr = topicToAddress(log.topics[2]);
    if (toAddr !== expectedRecipientLc) continue;
    let amount;
    try {
      amount = hexToBigInt(log.data);
    } catch {
      continue;
    }
    if (amount >= BigInt(expectedAmountAtomic)) {
      matched = true;
      break;
    }
  }
  if (!matched) {
    return { valid: false, reason: "no_matching_transfer_or_amount_too_low" };
  }
  const block = await rpc("eth_getBlockByNumber", [
    receipt.blockNumber,
    false
  ]);
  if (!block) return { valid: false, reason: "block_not_found" };
  const blockTs = Number(hexToBigInt(block.timestamp));
  const nowSec = Math.floor(Date.now() / 1e3);
  if (nowSec - blockTs > PROOF_MAX_AGE_SEC) {
    return { valid: false, reason: "proof_too_old" };
  }
  return { valid: true };
}
__name(verifyPaymentOnchain, "verifyPaymentOnchain");

// src/rate-limit.ts
var FREE_TIER_LIMIT = 100;
var POLAR_CHECKOUT_URL = "https://buy.polar.sh/polar_cl_zRn6I67zMjFuenkjDme5RCnDYmA3vefHqX1zG3A5Phh";
var inMemoryCounters = /* @__PURE__ */ new Map();
function todayUtc() {
  return (/* @__PURE__ */ new Date()).toISOString().slice(0, 10);
}
__name(todayUtc, "todayUtc");
function clientIp(request) {
  return request.headers.get("cf-connecting-ip") ?? request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
}
__name(clientIp, "clientIp");
async function checkRateLimit(ip, kv) {
  const key = `rl:${ip}:${todayUtc()}`;
  if (kv) {
    try {
      const raw2 = await kv.get(key, "text");
      const current2 = raw2 ? parseInt(raw2, 10) : 0;
      const next2 = current2 + 1;
      const allowed2 = current2 < FREE_TIER_LIMIT;
      if (allowed2) {
        await kv.put(key, String(next2), { expirationTtl: 9e4 });
      }
      return {
        allowed: allowed2,
        remaining: Math.max(0, FREE_TIER_LIMIT - next2),
        limit: FREE_TIER_LIMIT
      };
    } catch (e) {
      console.warn("rate-limit KV error, falling back to in-memory:", e.message);
    }
  }
  const current = inMemoryCounters.get(key) ?? 0;
  const next = current + 1;
  const allowed = current < FREE_TIER_LIMIT;
  if (allowed) {
    inMemoryCounters.set(key, next);
  }
  return {
    allowed,
    remaining: Math.max(0, FREE_TIER_LIMIT - next),
    limit: FREE_TIER_LIMIT
  };
}
__name(checkRateLimit, "checkRateLimit");
function rateLimitExceededResponse() {
  const body = JSON.stringify({
    error: "rate_limit_exceeded",
    message: `Free tier limit reached (${FREE_TIER_LIMIT} ops/day). Upgrade to unlimited at ${POLAR_CHECKOUT_URL}`,
    upgrade_url: POLAR_CHECKOUT_URL,
    tier: "free"
  });
  return new Response(body, {
    status: 429,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "x-ratelimit-limit": String(FREE_TIER_LIMIT),
      "x-ratelimit-remaining": "0",
      "retry-after": "86400"
    }
  });
}
__name(rateLimitExceededResponse, "rateLimitExceededResponse");
function withRateLimitHeaders(response, result) {
  const headers = new Headers(response.headers);
  headers.set("x-ratelimit-limit", String(result.limit));
  headers.set("x-ratelimit-remaining", String(result.remaining));
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
}
__name(withRateLimitHeaders, "withRateLimitHeaders");

// src/mcp-edge.ts
var JSON_HEADERS2 = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*"
};
function jsonrpcResult(id, result) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, result }), {
    status: 200,
    headers: { ...JSON_HEADERS2, "x-edge-source": "embedded-mcp" }
  });
}
__name(jsonrpcResult, "jsonrpcResult");
function jsonrpcError(id, code, message) {
  return new Response(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }), {
    status: 200,
    headers: JSON_HEADERS2
  });
}
__name(jsonrpcError, "jsonrpcError");
var EDGE_MCP_METHODS = /* @__PURE__ */ new Set([
  "initialize",
  "ping",
  "tools/list"
]);
var NONCE_PENDING_TTL_SEC = 600;
var NONCE_SPENT_TTL_SEC = 86400;
function payment402(requirements, failureReason) {
  const amountUsd = (Number(requirements.amount_atomic) / 1e6).toFixed(6);
  const body = {
    error: "payment_required",
    payment_requirements: requirements,
    instructions: `Pay ${amountUsd} USDC on Base to ${requirements.recipient}, then retry POST /mcp with header X-PAYMENT-PROOF: <tx-hash> and X-PAYMENT-NONCE: ${requirements.nonce}`
  };
  if (failureReason) body.previous_failure_reason = failureReason;
  return new Response(JSON.stringify(body), {
    status: 402,
    headers: { ...JSON_HEADERS2, "x-edge-source": "x402-gate" }
  });
}
__name(payment402, "payment402");
async function issue402(toolName, recipient, kv, failureReason) {
  const { requirements, nonce } = buildPaymentRequirements(toolName, recipient);
  try {
    await kv.put(`x402:nonce:${nonce}`, "pending", {
      expirationTtl: NONCE_PENDING_TTL_SEC
    });
  } catch (e) {
    console.warn("x402 nonce KV put failed:", e.message);
  }
  return payment402(requirements, failureReason);
}
__name(issue402, "issue402");
async function handleMcpRequest(request, originUrl, publicBaseUrl, x402Receiver, kv) {
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type, authorization, x-agent-identity, x-payment-proof, x-payment-nonce"
      }
    });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }
  const bodyText = await request.text();
  let body = {};
  try {
    body = JSON.parse(bodyText);
  } catch {
    return jsonrpcError(null, -32700, "Parse error");
  }
  const method = String(body.method ?? "");
  const id = body.id;
  if (!EDGE_MCP_METHODS.has(method)) {
    if (method === "tools/call") {
      const ip = clientIp(request);
      const rlResult = await checkRateLimit(ip, kv);
      if (!rlResult.allowed) {
        return rateLimitExceededResponse();
      }
      if (x402Receiver) {
        const toolName = String(body.params?.name ?? "");
        if (isPricedTool(toolName)) {
          const proof = request.headers.get("x-payment-proof");
          const nonce = request.headers.get("x-payment-nonce");
          if (!proof || !nonce) {
            return issue402(toolName, x402Receiver, kv);
          }
          if (!/^[0-9a-fA-F]{32}$/.test(nonce)) {
            return issue402(toolName, x402Receiver, kv, "nonce_malformed");
          }
          const kvKey2 = `x402:nonce:${nonce.toLowerCase()}`;
          const required = getRequiredAmount(toolName);
          const result = await verifyPaymentOnchain(
            proof,
            x402Receiver,
            required,
            kvKey2,
            kv
          );
          if (!result.valid) {
            return issue402(toolName, x402Receiver, kv, result.reason);
          }
          try {
            await kv.put(kvKey2, "spent", { expirationTtl: NONCE_SPENT_TTL_SEC });
          } catch (e) {
            console.warn("x402 mark-spent KV put failed:", e.message);
          }
        }
      }
      const proxyReq2 = new Request(request.url, {
        method: "POST",
        headers: request.headers,
        body: bodyText
      });
      const { response: response2 } = await proxyToOrigin(proxyReq2, originUrl);
      return withRateLimitHeaders(response2, rlResult);
    }
    const proxyReq = new Request(request.url, {
      method: "POST",
      headers: request.headers,
      body: bodyText
    });
    const { response } = await proxyToOrigin(proxyReq, originUrl);
    return response;
  }
  const snapshots = getSnapshots(publicBaseUrl);
  switch (method) {
    case "initialize": {
      const init = snapshots.mcpInitialize;
      return jsonrpcResult(id, init.result);
    }
    case "ping":
      return jsonrpcResult(id, {});
    case "tools/list": {
      const tl = snapshots.mcpToolsList;
      return jsonrpcResult(id, tl.result);
    }
    default:
      return jsonrpcError(id, -32601, `Method not found: ${method}`);
  }
}
__name(handleMcpRequest, "handleMcpRequest");

// src/alerts.ts
var REVENUE_COOLDOWN_MS = 24 * 60 * 60 * 1e3;
var BALANCE_COOLDOWN_MS = 12 * 60 * 60 * 1e3;
var BALANCE_THRESHOLD_USD = 2;
var MILESTONES = [1e3, 5e3, 1e4, 5e4, 1e5];
var KV_FIRST_REVENUE_FIRED = "alert:first_revenue:fired_at";
var KV_TWILIO_LOW_FIRED = "alert:twilio_low:fired_at";
var KV_VAPI_LOW_FIRED = "alert:vapi_low:fired_at";
var kvMilestoneKey = /* @__PURE__ */ __name((n) => `alert:milestone:${n}`, "kvMilestoneKey");
function toFiniteNumber(v) {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}
__name(toFiniteNumber, "toFiniteNumber");
async function readLiveMetrics(kv) {
  try {
    const raw2 = await kv.get("live:metrics", "text");
    if (!raw2) return {};
    const parsed = JSON.parse(raw2);
    if (parsed && typeof parsed === "object") return parsed;
    return {};
  } catch {
    return {};
  }
}
__name(readLiveMetrics, "readLiveMetrics");
async function fetchExternalHealth(originUrl) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8e3);
    const r = await fetch(originUrl + "/healthz/external", {
      headers: { "x-edge-probe": "cron-alerts" },
      signal: ctrl.signal
    });
    clearTimeout(t);
    if (!r.ok) return null;
    const body = await r.json();
    if (body && typeof body === "object") return body;
    return null;
  } catch {
    return null;
  }
}
__name(fetchExternalHealth, "fetchExternalHealth");
function extractBalance(health, name) {
  if (!health) return null;
  const services = health.services ?? health.external?.services;
  if (!services || typeof services !== "object") return null;
  const svc = services[name];
  if (!svc || typeof svc !== "object") return null;
  return toFiniteNumber(svc.balance);
}
__name(extractBalance, "extractBalance");
function fmtUtc(now) {
  const d = new Date(now);
  const pad = /* @__PURE__ */ __name((n) => String(n).padStart(2, "0"), "pad");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(
    d.getUTCMinutes()
  )} UTC`;
}
__name(fmtUtc, "fmtUtc");
async function sendTelegram(token, chatId, text) {
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "Markdown",
        disable_web_page_preview: true
      })
    });
    if (!r.ok) {
      console.warn("telegram sendMessage failed:", r.status, await r.text().catch(() => ""));
      return false;
    }
    return true;
  } catch (e) {
    console.warn("telegram sendMessage threw:", e.message);
    return false;
  }
}
__name(sendTelegram, "sendTelegram");
async function readFiredAt(kv, key) {
  try {
    const v = await kv.get(key, "text");
    if (!v) return 0;
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  } catch {
    return 0;
  }
}
__name(readFiredAt, "readFiredAt");
async function recordFiredAt(kv, key, now) {
  try {
    await kv.put(key, String(now));
  } catch (e) {
    console.warn(`KV put ${key} failed:`, e.message);
  }
}
__name(recordFiredAt, "recordFiredAt");
async function recordSentinel(kv, key, now) {
  try {
    await kv.put(key, String(now));
  } catch (e) {
    console.warn(`KV put ${key} failed:`, e.message);
  }
}
__name(recordSentinel, "recordSentinel");
async function milestoneHasFired(kv, n) {
  try {
    const v = await kv.get(kvMilestoneKey(n), "text");
    return v !== null && v !== "";
  } catch {
    return false;
  }
}
__name(milestoneHasFired, "milestoneHasFired");
async function runAlertChecks(env2, kv) {
  const token = env2.TELEGRAM_BOT_TOKEN;
  const chatId = env2.TELEGRAM_CHAT_ID;
  if (!token || !chatId) return;
  const metricsUrl = (env2.PUBLIC_BASE_URL ?? "https://agent-broker-edge.basil-agent.workers.dev") + "/api/metrics";
  const now = Date.now();
  const ts = fmtUtc(now);
  const metrics = await readLiveMetrics(kv);
  const businessesFound = toFiniteNumber(metrics.total_businesses_found) ?? 0;
  const messagesSent = toFiniteNumber(metrics.total_messages_sent) ?? 0;
  if (businessesFound > 0 || messagesSent > 0) {
    const firedAt = await readFiredAt(kv, KV_FIRST_REVENUE_FIRED);
    if (now - firedAt > REVENUE_COOLDOWN_MS) {
      const which = messagesSent > 0 ? "messages_sent" : "businesses_found";
      const count = messagesSent > 0 ? messagesSent : businessesFound;
      const msg = `*Agent Broker* \u2014 first \`${which}\`!
Count: *${count}*
Time: ${ts}
` + metricsUrl;
      const ok = await sendTelegram(token, chatId, msg);
      if (ok) await recordFiredAt(kv, KV_FIRST_REVENUE_FIRED, now);
    }
  }
  const twilioFiredAt = await readFiredAt(kv, KV_TWILIO_LOW_FIRED);
  const vapiFiredAt = await readFiredAt(kv, KV_VAPI_LOW_FIRED);
  const twilioOnCooldown = now - twilioFiredAt <= BALANCE_COOLDOWN_MS;
  const vapiOnCooldown = now - vapiFiredAt <= BALANCE_COOLDOWN_MS;
  if (!twilioOnCooldown || !vapiOnCooldown) {
    const health = await fetchExternalHealth(env2.ORIGIN_URL);
    if (!twilioOnCooldown) {
      const twilioBal = extractBalance(health, "twilio");
      if (twilioBal !== null && twilioBal < BALANCE_THRESHOLD_USD) {
        const msg = `*Agent Broker* \u2014 Twilio low balance!
Balance: *$${twilioBal.toFixed(2)}* (threshold $${BALANCE_THRESHOLD_USD.toFixed(2)})
Time: ${ts}
Top up: https://console.twilio.com/`;
        const ok = await sendTelegram(token, chatId, msg);
        if (ok) await recordFiredAt(kv, KV_TWILIO_LOW_FIRED, now);
      }
    }
    if (!vapiOnCooldown) {
      const vapiBal = extractBalance(health, "vapi");
      if (vapiBal !== null && vapiBal < BALANCE_THRESHOLD_USD) {
        const msg = `*Agent Broker* \u2014 Vapi low balance!
Balance: *$${vapiBal.toFixed(2)}* (threshold $${BALANCE_THRESHOLD_USD.toFixed(2)})
Time: ${ts}
Top up: https://dashboard.vapi.ai/`;
        const ok = await sendTelegram(token, chatId, msg);
        if (ok) await recordFiredAt(kv, KV_VAPI_LOW_FIRED, now);
      }
    }
  }
  const agentsRequested = toFiniteNumber(metrics.total_agents_requested) ?? 0;
  if (agentsRequested > 0) {
    for (const threshold of MILESTONES) {
      if (agentsRequested < threshold) break;
      if (await milestoneHasFired(kv, threshold)) continue;
      const msg = `*Agent Broker* \u2014 traffic milestone reached!
\`total_agents_requested\` >= *${threshold.toLocaleString("en-US")}*
Current: *${agentsRequested.toLocaleString("en-US")}*
Time: ${ts}
` + metricsUrl;
      const ok = await sendTelegram(token, chatId, msg);
      if (ok) await recordSentinel(kv, kvMilestoneKey(threshold), now);
    }
  }
}
__name(runAlertChecks, "runAlertChecks");

// src/index.ts
var app = new Hono2();
function publicBaseUrlOf(c) {
  if (c.env.PUBLIC_BASE_URL) return c.env.PUBLIC_BASE_URL;
  const u = new URL(c.req.url);
  return `${u.protocol}//${u.host}`;
}
__name(publicBaseUrlOf, "publicBaseUrlOf");
app.get("/edge/health", (c) => {
  return c.json({
    status: "healthy",
    edge: "cloudflare-workers",
    version: c.env.EDGE_VERSION,
    public_url: publicBaseUrlOf(c),
    timestamp: (/* @__PURE__ */ new Date()).toISOString()
  });
});
app.get("/edge/info", async (c) => {
  let originStatus;
  let originLatencyMs = null;
  const start = Date.now();
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 5e3);
    const r = await fetch(c.env.ORIGIN_URL + "/health", {
      headers: { "x-edge-probe": "info" },
      signal: ctrl.signal
    });
    clearTimeout(t);
    originLatencyMs = Date.now() - start;
    originStatus = r.ok ? "healthy" : `unhealthy_${r.status}`;
  } catch {
    originStatus = "unreachable";
  }
  return c.json({
    edge: {
      version: c.env.EDGE_VERSION,
      service: c.env.SERVICE_NAME,
      runtime: "cloudflare-workers",
      mode: "edge-first"
    },
    origin: {
      url: c.env.ORIGIN_URL,
      status: originStatus,
      latency_ms: originLatencyMs,
      role: "state-changing operations only"
    },
    discovery_served_from: "embedded snapshots + KV live overlay",
    timestamp: (/* @__PURE__ */ new Date()).toISOString()
  });
});
app.all("/mcp", async (c) => {
  return handleMcpRequest(
    c.req.raw,
    c.env.ORIGIN_URL,
    publicBaseUrlOf(c),
    c.env.X402_RECEIVER_ADDRESS,
    c.env.CACHE
  );
});
app.get("/health", async (c) => {
  return c.json({
    status: "healthy",
    timestamp: (/* @__PURE__ */ new Date()).toISOString(),
    checks: { manifest: "ok", directory: "ok", compliance: "ok" },
    edge: "cloudflare-workers"
  });
});
app.get("/api/metrics", async (c) => {
  const cached2 = await c.env.CACHE.get("live:metrics", "text");
  if (cached2) {
    const meta = await c.env.CACHE.get("live:metrics:ts", "text");
    return new Response(cached2, {
      status: 200,
      headers: {
        "content-type": "application/json",
        "x-edge-source": "kv-cached",
        "x-edge-age": meta ? String(Math.floor((Date.now() - Number(meta)) / 1e3)) : "0"
      }
    });
  }
  const { response } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  if (response.ok) {
    const body = await response.clone().text();
    try {
      await c.env.CACHE.put("live:metrics", body, { expirationTtl: 60 });
      await c.env.CACHE.put("live:metrics:ts", String(Date.now()), { expirationTtl: 60 });
    } catch (e) {
      console.warn("KV put live:metrics failed:", e.message);
    }
    return new Response(body, {
      status: 200,
      headers: {
        "content-type": "application/json",
        "x-edge-source": "origin-fresh"
      }
    });
  }
  return response;
});
function oauthMetadata(baseUrl) {
  return {
    issuer: baseUrl,
    authorization_endpoint: null,
    token_endpoint: null,
    registration_endpoint: null,
    response_types_supported: [],
    grant_types_supported: [],
    scopes_supported: [],
    token_endpoint_auth_methods_supported: ["none"],
    authorization_required: false,
    service_documentation: `${baseUrl}/manifest`
  };
}
__name(oauthMetadata, "oauthMetadata");
app.get("/.well-known/oauth-authorization-server", (c) => {
  return new Response(JSON.stringify(oauthMetadata(publicBaseUrlOf(c))), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=86400",
      "x-edge-source": "edge-stub"
    }
  });
});
app.get("/.well-known/oauth-protected-resource", (c) => {
  const baseUrl = publicBaseUrlOf(c);
  return new Response(
    JSON.stringify({
      resource: baseUrl,
      authorization_servers: [],
      bearer_methods_supported: [],
      resource_documentation: `${baseUrl}/manifest`,
      authentication_required: false
    }),
    {
      status: 200,
      headers: {
        "content-type": "application/json",
        "cache-control": "public, max-age=86400",
        "x-edge-source": "edge-stub"
      }
    }
  );
});
app.get("/.well-known/openid-configuration", (c) => {
  return new Response(JSON.stringify(oauthMetadata(publicBaseUrlOf(c))), {
    status: 200,
    headers: {
      "content-type": "application/json",
      "cache-control": "public, max-age=86400",
      "x-edge-source": "edge-stub"
    }
  });
});
app.get("/healthz/external", async (c) => {
  const { response } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  return response;
});
app.all("*", async (c) => {
  const publicBaseUrl = publicBaseUrlOf(c);
  const discovered = await tryServeDiscovery(c.req.raw, publicBaseUrl, c.env.CACHE);
  if (discovered !== null) return discovered;
  const { response, attempts, totalMs, retried } = await proxyToOrigin(c.req.raw, c.env.ORIGIN_URL);
  const headers = new Headers(response.headers);
  headers.set("x-edge-source", "origin-proxy");
  headers.set("x-edge-attempts", String(attempts));
  headers.set("x-edge-retried", String(retried));
  headers.set("server-timing", `origin;dur=${totalMs}`);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
});
var REFRESH_TARGETS = [
  { path: "/manifest", isJson: true },
  { path: "/.well-known/agents.json", isJson: true },
  { path: "/.well-known/anthropic-tools.json", isJson: true },
  { path: "/.well-known/openai-tools.json", isJson: true },
  { path: "/.well-known/agent-service", isJson: true },
  { path: "/.well-known/ai-plugin.json", isJson: true },
  { path: "/.well-known/mcp.json", isJson: true },
  { path: "/llms.txt", isJson: false },
  { path: "/supply/platforms", isJson: true },
  { path: "/compliance/jurisdictions", isJson: true }
];
async function scheduledHandler(event, env2, ctx) {
  const publicBaseUrl = env2.PUBLIC_BASE_URL ?? "https://agent-broker-edge.basil-agent.workers.dev";
  const tasks = [];
  tasks.push(
    fetch(env2.ORIGIN_URL + "/health", { headers: { "x-edge-probe": "cron-warmup" } }).catch(() => null)
  );
  const minute = new Date(event.scheduledTime).getUTCMinutes();
  const shouldRefreshKv = minute % 30 === 0;
  if (shouldRefreshKv) {
    for (const { path, isJson } of REFRESH_TARGETS) {
      tasks.push(refreshKvFromOrigin(env2.CACHE, env2.ORIGIN_URL, publicBaseUrl, path, isJson));
    }
    tasks.push(
      (async () => {
        try {
          const r = await fetch(env2.ORIGIN_URL + "/api/metrics", {
            headers: { "x-edge-probe": "cron-metrics" }
          });
          if (r.ok) {
            const body = await r.text();
            await env2.CACHE.put("live:metrics", body, { expirationTtl: 60 });
            await env2.CACHE.put("live:metrics:ts", String(Date.now()), { expirationTtl: 60 });
          }
        } catch (e) {
          console.warn("cron metrics refresh failed:", e.message);
        }
        try {
          await runAlertChecks(env2, env2.CACHE);
        } catch (e) {
          console.warn("cron alerts failed:", e.message);
        }
      })()
    );
  }
  ctx.waitUntil(Promise.all(tasks));
}
__name(scheduledHandler, "scheduledHandler");
var index_default = {
  fetch: app.fetch,
  scheduled: scheduledHandler
};
export {
  index_default as default
};
//# sourceMappingURL=index.js.map
