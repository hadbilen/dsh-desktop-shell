/**
 * dsh-notify — Client (browser) half.
 *
 * Job: watch three events in a DSH session and raise a desktop notification.
 *
 *   1. Turn finished  -> on the `api-session/status` (sessionId, running) event,
 *                        the running true -> false transition.
 *   2. Question /     -> the `approval/request` waterfall. `next()` is called
 *      approval           AFTER the notification is raised; the order matters,
 *                        otherwise the approval flow deadlocks.
 *   3. Agent error    -> the `api-session/error` event.
 *
 * Notifications use the browser `Notification` API. QtWebEngine and Chrome
 * bridge that call to the operating system's notification service, which avoids
 * the `notify-send` plus session-bus (DBUS_SESSION_BUS_ADDRESS) problem and
 * makes click-to-focus possible.
 *
 * This file is wrapped in `window.__ModuleLoader__.load({...})`; the DSH client
 * plugin loader expects exactly this shape.
 */

window.__ModuleLoader__.load({
	id: "dsh-notify",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

		//#region settings
		/**
		 * User settings come from the host-side `notify` namespace. If they
		 * cannot be read these defaults apply; they must stay in sync with
		 * DEFAULTS in `lib/index.js`.
		 */
		const DEFAULTS = {
			onComplete: true,
			onQuestion: true,
			onError: true,
			onlyWhenHidden: true,
			includeSubagents: false,
			sound: false
		};

		/** Effective settings; updated once the host configuration is read. */
		let settings = { ...DEFAULTS };

		/**
		 * Read the host configuration and apply it to `settings`.
		 *
		 * If reading fails the defaults silently remain: notifications keep
		 * working and only the user's preferences are ignored. That keeps the
		 * feature from disappearing entirely because of a bad configuration.
		 *
		 * @param ctx - Client plugin context.
		 */
		function loadSettings(ctx) {
			try {
				const config = ctx.config ?? {};
				for (const key of Object.keys(DEFAULTS)) {
					const value = config[key];
					// Cordis config values may be wrapped (getter/observable);
					// resolve scalar and boolean values safely.
					const raw = value && typeof value === "object" && "get" in value
						? value.get()
						: value;
					if (typeof raw === typeof DEFAULTS[key]) settings[key] = raw;
				}
			} catch {
				// Settings unreadable: continue with the defaults.
			}
		}
		//#endregion

		//#region notification core
		/** Last known running state per session. */
		const runningState = new Map();

		/** Last notification timestamp per session, used to suppress repeats. */
		const lastNotified = new Map();

		/** Minimum time between two notifications for the same session (ms). */
		const DEBOUNCE_MS = 2000;

		/** Has the browser granted notification permission? */
		function permissionGranted() {
			return typeof Notification !== "undefined"
				&& Notification.permission === "granted";
		}

		/**
		 * Request notification permission. Asked only once; if the browser has
		 * already decided, this call does nothing.
		 */
		function requestPermission() {
			if (typeof Notification === "undefined") return;
			if (Notification.permission !== "default") return;
			try {
				// The promise-returning modern signature; a failure is harmless.
				const result = Notification.requestPermission();
				if (result && typeof result.catch === "function") result.catch(() => {});
			} catch {
				// Legacy callback signature or a blocked context: ignore.
			}
		}

		/** Is the window in front? It must be visible and focused. */
		function windowInForeground() {
			try {
				return document.visibilityState === "visible" && document.hasFocus();
			} catch {
				return false;
			}
		}

		/**
		 * Raise a notification.
		 *
		 * @param key - Debounce key, usually the session id.
		 * @param title - Notification title.
		 * @param body - Notification body.
		 * @param tag - Browser-side tag that groups identical notifications.
		 */
		function notify(key, title, body, tag) {
			if (!permissionGranted()) return;
			if (settings.onlyWhenHidden && windowInForeground()) return;

			const now = Date.now();
			const previous = lastNotified.get(key) ?? 0;
			if (now - previous < DEBOUNCE_MS) return;
			lastNotified.set(key, now);

			try {
				const notification = new Notification(title, {
					body,
					tag,
					// The sound preference belongs to the user; `silent: false`
					// uses the system default sound.
					silent: !settings.sound
				});
				notification.onclick = () => {
					try {
						window.focus();
						notification.close();
					} catch {
						// The window could not be focused; the notification still closes.
					}
				};
			} catch {
				// The notification could not be created (permission may be revoked).
			}
		}

		/** Flatten text for a notification body (single line, max 140 chars). */
		function summarize(text, limit = 140) {
			if (typeof text !== "string") return "";
			const flat = text.replace(/\s+/g, " ").trim();
			return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
		}

		/**
		 * Is this session in the main view, or is it a subagent session?
		 *
		 * Main-view sessions are the ones the user is actually looking at.
		 * Subagents are excluded by default: otherwise every subagent turn
		 * raises a notification and the feature becomes unusable.
		 *
		 * @param ctx - Client context.
		 * @param sessionId - Session being queried.
		 * @returns true when in the main view; true when unknown (fail safe).
		 */
		function isMainSession(ctx, sessionId) {
			try {
				const list = ctx.uiSession?.sessions?.list?.getSnapshot?.();
				const row = list?.byId?.[sessionId];
				if (row === undefined) return true;
				return (row.retainedBy?.mainView ?? 0) > 0;
			} catch {
				return true;
			}
		}

		/** Resolve a session title; fall back to a short id when unavailable. */
		function sessionTitle(ctx, sessionId) {
			try {
				const list = ctx.uiSession?.sessions?.list?.getSnapshot?.();
				const row = list?.byId?.[sessionId];
				const title = row?.title;
				if (typeof title === "string" && title.trim() !== "") return title.trim();
			} catch {
				// The title could not be read.
			}
			const short = String(sessionId ?? "").slice(0, 8);
			return short ? `Session ${short}` : "DeepSeek Harness";
		}
		//#endregion

		//#region plugin
		const name = "dsh-notify";

		/**
		 * Client-side service dependencies. `uiSession` supplies the session
		 * list and main-view information; without it the plugin still runs and
		 * treats every session as a main session.
		 */
		const inject = ["remote"];

		/**
		 * Attach the plugin.
		 *
		 * @param ctx - Client plugin context.
		 */
		function apply(ctx) {
			loadSettings(ctx);
			requestPermission();

			// --- 1) Turn finished ----------------------------------------------
			if (settings.onComplete) {
				ctx.effect(() => ctx.remote.$on("api-session/status", (sessionId, running) => {
					const previous = runningState.get(sessionId);
					runningState.set(sessionId, running);

					// Notify only on a running -> stopped transition. On the
					// first observation (previous === undefined) we do not
					// notify for a past turn; otherwise every page reload would
					// raise a notification.
					if (running || previous !== true) return;

					if (!settings.includeSubagents && !isMainSession(ctx, sessionId)) return;

					notify(
						sessionId,
						"Reply ready",
						sessionTitle(ctx, sessionId),
						`dsh-complete-${sessionId}`
					);
				}), "dsh-notify: turn finished");
			}

			// --- 2) Question / approval ---------------------------------------
			if (settings.onQuestion) {
				ctx.effect(() => ctx.remote.$on("approval/request", (request, next) => {
					// CRITICAL: `next()` must be called after raising the
					// notification. Otherwise the approval flow never receives
					// an answer and the agent deadlocks.
					try {
						const agent = request?.agent;
						const sessionId = agent?.id ?? "approval";
						if (settings.includeSubagents || isMainSession(ctx, sessionId)) {
							const reason = summarize(request?.reason ?? request?.toolName ?? "");
							notify(
								sessionId,
								"Input needed",
								reason || sessionTitle(ctx, sessionId),
								`dsh-approval-${sessionId}`
							);
							runningState.set(sessionId, true);
						}
					} catch {
						// A notification failure must never block the approval flow.
					}
					return next();
				}), "dsh-notify: approval request");
			}

			// --- 3) Agent error -----------------------------------------------
			if (settings.onError) {
				ctx.effect(() => ctx.remote.$on("api-session/error", (sessionId, error) => {
					if (!settings.includeSubagents && !isMainSession(ctx, sessionId)) return;
					notify(
						sessionId,
						"DSH: error",
						summarize(String(error ?? "")) || sessionTitle(ctx, sessionId),
						`dsh-error-${sessionId}`
					);
				}), "dsh-notify: agent error");
			}
		}
		//#endregion

		exports.apply = apply;
		exports.inject = inject;
		exports.name = name;
		return module.exports;
	}
});
