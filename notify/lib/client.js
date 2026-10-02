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
 *
 * It also contributes the General-settings row ("Notifications", one switch per
 * preference): DSH 0.2 ships no generic form for a plugin namespace, so the row
 * is what makes the settings editable in the interface.
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

		/** Effective settings; replaced whenever the settings document changes. */
		let settings = { ...DEFAULTS };

		/**
		 * Read the client plugin's own `config`, the legacy path.
		 *
		 * Kept as a fallback only: a boot-mounted client module is created with
		 * just its id (no `config`), so in a real DSH session this is always empty
		 * and the defaults below stay in force. `adoptFormSettings` is the path
		 * that actually carries the user's choices.
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

		/**
		 * Adopt the effective settings from DSH's settings document.
		 *
		 * The `notify` namespace is declared by the host half with volatile fields,
		 * which is what makes DSH render it as an editable form; the values are read
		 * through the shared `configForms` service — `ctx.configForms.get("<entry
		 * id>")`, the same way DSH's own `ui-theme` plugin reads its preferences.
		 * `getSnapshot().value` is the namespace section (`undefined` until the
		 * mirror is ready) and `subscribe` delivers every later change, so a toggle
		 * takes effect without reloading the page.
		 *
		 * Anything unexpected — no settings provider, an unreadable namespace, a
		 * value of the wrong type — leaves `DEFAULTS` in force: a settings problem
		 * must never break notifications.
		 *
		 * @param ctx - Client plugin context.
		 * @returns true when the namespace was reachable and adopted.
		 */
		function adoptFormSettings(ctx) {
			const form = ctx.configForms?.get?.("notify");
			if (!form || typeof form.getSnapshot !== "function") return false;

			const adopt = (snapshot) => {
				const section = snapshot?.value;
				const next = { ...DEFAULTS };
				if (section && typeof section === "object") {
					for (const key of Object.keys(DEFAULTS)) {
						const raw = section[key];
						// A volatile field resolves to `{}` until the settings
						// document supplies a value, so only a matching primitive
						// is adopted.
						if (typeof raw === typeof DEFAULTS[key]) next[key] = raw;
					}
				}
				settings = next;
			};

			adopt(form.getSnapshot());
			if (typeof form.subscribe === "function") {
				ctx.effect(() => form.subscribe((snapshot) => adopt(snapshot)),
					"dsh-notify: settings");
			}
			return true;
		}

		/**
		 * Apply the user's settings, with `DEFAULTS` as the safety net.
		 *
		 * @param ctx - Client plugin context.
		 */
		function applySettings(ctx) {
			// Start from the documented defaults on every adoption, so a namespace
			// that cannot be read can never inherit values from an earlier read.
			settings = { ...DEFAULTS };
			loadSettings(ctx);
			let adopted = false;
			try {
				adopted = adoptFormSettings(ctx);
			} catch {
				adopted = false;
			}
			if (adopted) return;
			// The settings provider may be composed after this module; adopt the
			// namespace as soon as it appears.
			ctx.inject(["configForms"], (child) => {
				try {
					adoptFormSettings(child);
				} catch {
					// Keep the defaults.
				}
			});
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

		//#region settings row
		/**
		 * Settings-page slot this row registers into. DSH's own preference rows use
		 * the same one (`ui-theme` for Appearance, `ui-settings-session-log` for the
		 * log-upload switch), so the row shows up on the General settings page.
		 *
		 * Why a custom row exists at all: DSH 0.2.0-rc.2 ships no generic form for a
		 * plugin namespace — the host sends an `autoGenerate` flag but no client code
		 * consumes it, and the "Built-in plugins" page only lists entries. Every
		 * first-party plugin with editable settings draws its own row; this is ours.
		 */
		const ROW_SLOT = "settings.general.item";
		/** Locale namespace of the row copy. */
		const ROW_LOCALE = "settings.notify";
		/** Patch entry id — also the settings namespace this row edits. */
		const ROW_NAMESPACE = "notify";
		/** Row fields in display order; each one is a boolean in the namespace. */
		const ROW_FIELDS = Object.freeze([
			"onComplete", "onQuestion", "onError",
			"onlyWhenHidden", "includeSubagents", "sound"
		]);
		/** Row copy. DSH ships en/zh; `tr` is included for completeness. */
		const ROW_COPY = {
			en: {
				title: "Notifications",
				description: "Desktop notifications for replies, questions and failures.",
				onComplete: "Notify when a reply completes",
				onQuestion: "Notify when input is needed",
				onError: "Notify when a run fails",
				onlyWhenHidden: "Only while the window is not focused",
				includeSubagents: "Include subagent sessions",
				sound: "Play the notification sound",
				saved: "Preference saved",
				failed: "Could not save preference",
				statusLoading: "Waiting for the settings document…",
				statusNotServed: "This deployment does not serve the \"notify\" settings namespace.",
				statusUnknown: "Settings state unknown."
			},
			zh: {
				title: "通知",
				description: "回复完成、需要输入和运行失败时的桌面通知。",
				onComplete: "回复完成时通知",
				onQuestion: "需要输入时通知",
				onError: "运行失败时通知",
				onlyWhenHidden: "仅在窗口未聚焦时",
				includeSubagents: "包含子代理会话",
				sound: "播放通知声音",
				saved: "设置已保存",
				failed: "无法保存设置",
				statusLoading: "正在读取设置…",
				statusNotServed: "此部署未提供 notify 设置命名空间。",
				statusUnknown: "设置状态未知。"
			},
			tr: {
				title: "Bildirimler",
				description: "Yanıt, soru ve hata olaylarında masaüstü bildirimi.",
				onComplete: "Yanıt tamamlandığında bildir",
				onQuestion: "Girdi gerektiğinde bildir",
				onError: "Çalışma hata verdiğinde bildir",
				onlyWhenHidden: "Yalnızca pencere ön planda değilken",
				includeSubagents: "Alt-ajan oturumlarını da kat",
				sound: "Bildirim sesini çal",
				saved: "Ayar kaydedildi",
				failed: "Ayar kaydedilemedi",
				statusLoading: "Ayar belgesi bekleniyor…",
				statusNotServed: "Bu kurulum \"notify\" ayar ad alanını sunmuyor.",
				statusUnknown: "Ayar durumu bilinmiyor."
			}
		};
		/** Row layout. Class names are namespaced so they cannot collide. */
		const ROW_CSS = ".dsh-notify-row{border-bottom:.5px solid var(--dsw-alias-border-l2);display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:flex-start;padding:16px 0}.dsh-notify-row__title{font-size:14px;line-height:20px}.dsh-notify-row__description{color:var(--dsw-alias-label-secondary);margin-top:4px;font-size:12px;line-height:18px}.dsh-notify-row__fields{display:flex;flex-direction:column;gap:12px;min-width:280px}.dsh-notify-row__field{display:flex;align-items:center;justify-content:space-between;gap:16px}.dsh-notify-row__fieldLabel{font-size:13px;line-height:18px}.dsh-notify-row__notice{flex-basis:100%;color:var(--dsw-alias-label-secondary);font-size:12px;line-height:18px}";
		const ROW_CSS_ID = "dsh-notify/NotificationsRow.css";

		/** React runtime and DSH primitives, resolved when the row is registered. */
		let rowUi = null;

		/** Inject the row stylesheet once (styling is cosmetic). */
		function injectRowStyles() {
			try {
				if (typeof document === "undefined") return;
				if (document.querySelector(`style[data-plugin-css="${ROW_CSS_ID}"]`) !== null) return;
				const tag = document.createElement("style");
				tag.dataset.plugin = "dsh-notify";
				tag.dataset.pluginCss = ROW_CSS_ID;
				tag.textContent = ROW_CSS;
				document.head.appendChild(tag);
			} catch {
				// Without the stylesheet the row still works.
			}
		}

		/**
		 * The General-settings row: one switch per notification preference.
		 *
		 * Props come from the slot registration: `useForm`/`useNotice` are hooks over
		 * the snapshot stores named in `hooks`, `save` writes through the settings
		 * document, `status` explains why the switches are disabled and `t` resolves
		 * the row copy.
		 *
		 * The row is always rendered — also before, or without, a served settings
		 * namespace. A row that silently disappears cannot be diagnosed, so an unready
		 * namespace is reported as text instead.
		 *
		 * @param props - slot-provided hooks, writer, status and translator.
		 * @returns the preference row.
		 */
		function NotificationsRow(props) {
			const form = props.useForm((value) => value);
			// The selector must pick the FIELD: returning the whole store state would
			// hand an object to React as a child (error #31) and the slot entry would
			// be dropped — the row disappeared without a trace.
			const notice = props.useNotice((value) => value?.notice);
			const ready = form?.status === "ready" && form?.writable === true;
			const value = form?.value ?? {};
			const control = (key) => {
				const checked = value[key] === true;
				const onChange = (next) => props.save(key, next);
				const label = props.t(key);
				// The DSH Switch primitive renders `label` as an aria-label ONLY, so the
				// visible text is drawn here — the same way DSH's own settings rows do it.
				const toggle = rowUi.Switch
					? rowUi.jsx(rowUi.Switch, { checked, label, disabled: !ready, onChange })
					: rowUi.jsx("input", {
						type: "checkbox",
						checked,
						"aria-label": label,
						disabled: !ready,
						onChange: (event) => onChange(event.target.checked === true)
					});
				return rowUi.jsx("div", {
					key,
					className: "dsh-notify-row__field",
					children: [
						rowUi.jsx("span", { className: "dsh-notify-row__fieldLabel", children: label }),
						toggle
					]
				});
			};
			const children = [
				rowUi.jsx("div", {
					className: "dsh-notify-row__text",
					children: [
						rowUi.jsx("div", { className: "dsh-notify-row__title", children: props.t("title") }),
						rowUi.jsx("div", { className: "dsh-notify-row__description", children: props.t("description") })
					]
				}),
				rowUi.jsx("div", {
					className: "dsh-notify-row__fields",
					children: ROW_FIELDS.map(control)
				})
			];
			const note = ready
				? (typeof notice === "string" && notice !== "" ? notice : null)
				: props.status();
			if (note) {
				children.push(rowUi.jsx("div", {
					className: "dsh-notify-row__notice",
					children: props.t(note)
				}));
			}
			return rowUi.jsx("div", { className: "dsh-notify-row", children });
		}

		/**
		 * Register the settings row when the settings UI is composed.
		 *
		 * Every dependency is optional and every failure is swallowed: a composition
		 * without the settings page, without React, or without the primitives simply
		 * has no row — notifications keep working, which is the whole point of the
		 * plugin.
		 *
		 * @param ctx - Client context carrying slots/locale/configForms.
		 * @returns true when the row was registered.
		 */
		function registerSettingsRow(ctx) {
			if (typeof ctx.slots?.register !== "function") return false;
			if (typeof ctx.locale?.register !== "function") return false;
			const form = ctx.configForms?.get?.(ROW_NAMESPACE);
			if (!form || typeof form.getSnapshot !== "function" || typeof form.set !== "function") return false;

			let ui;
			let store = null;
			try {
				ui = { jsx: require("react/jsx-runtime").jsx };
				if (typeof ui.jsx !== "function") return false;
			} catch {
				return false;
			}
			try {
				// Optional: without the primitive the row uses native checkboxes.
				ui.Switch = require("@deepseek-ai/dsh-client-ui-primitives").Switch ?? null;
			} catch {
				ui.Switch = null;
			}
			try {
				store = require("@deepseek-ai/dsh-client-store").createSnapshotStore({ notice: null, sequence: 0 });
			} catch {
				store = null;
			}

			const save = async (key, value) => {
				if (!ROW_FIELDS.includes(key)) return false;
				let accepted = false;
				try {
					accepted = await form.set(key, value);
				} catch {
					accepted = false;
				}
				if (store) {
					store.update((state) => {
						state.notice = accepted ? "saved" : "failed";
						state.sequence++;
					});
				}
				return accepted;
			};

			/**
			 * Why the switches are not usable yet, as a copy key.
			 *
			 * `describe()` is the shared settings mirror: when the Host serves the
			 * namespace it is listed there, so a missing entry distinguishes "the
			 * deployment does not serve this namespace" from "still loading".
			 *
			 * @returns the status key shown under the row.
			 */
			const status = () => {
				let served = null;
				try {
					const view = ctx.configForms?.describe?.().getSnapshot?.().view;
					if (Array.isArray(view?.namespaces)) {
						served = view.namespaces.some((row) => row?.ns === ROW_NAMESPACE);
					}
				} catch {
					served = null;
				}
				if (served === false) return "statusNotServed";
				if (served === null) return "statusUnknown";
				return "statusLoading";
			};

			rowUi = ui;
			injectRowStyles();
			ctx.effect(() => ctx.locale.register(ROW_LOCALE, ROW_COPY),
				"dsh-notify: settings row copy");
			// Registered directly, NOT gated by `whileServed`: the row reports its own
			// readiness instead of vanishing, which is what makes a missing row
			// diagnosable from the interface.
			ctx.effect(() => ctx.slots.inject(ROW_SLOT, () => ctx.slots.register({
				name: ROW_SLOT,
				id: ROW_NAMESPACE,
				order: 90,
				locale: ROW_LOCALE,
				inject: () => ({
					hooks: store ? { form, notice: store } : { form },
					save,
					status
				})
			}, NotificationsRow)), "dsh-notify: settings row");
			return true;
		}
		//#endregion

		//#region plugin
		const name = "dsh-notify";

		/**
		 * Client-side service dependencies. `remote` carries the session events;
		 * `uiSession` supplies the session list and main-view information and is
		 * optional (without it the plugin still runs and treats every session as a
		 * main session). `configForms` is not listed here on purpose: the settings
		 * namespace is adopted through an optional inject so a composition without
		 * the settings provider still loads this plugin.
		 */
		const inject = ["remote"];

		/**
		 * Attach the plugin.
		 *
		 * The three event handlers are registered unconditionally and each one
		 * checks its own setting when it fires: that way a toggle in the settings
		 * page takes effect immediately, without reloading the page.
		 *
		 * @param ctx - Client plugin context.
		 */
		function apply(ctx) {
			applySettings(ctx);
			requestPermission();

			// Settings row: optional on purpose. It needs the settings page, React and
			// the UI primitives; when any of them is missing the row is skipped and the
			// notifications keep working with the values already adopted above.
			ctx.inject(["slots", "locale", "configForms"], (child) => {
				try {
					registerSettingsRow(child);
				} catch {
					// A settings-UI problem must never disable notifications.
				}
			});

			// --- 1) Turn finished ----------------------------------------------
			ctx.effect(() => ctx.remote.$on("api-session/status", (sessionId, running) => {
				const previous = runningState.get(sessionId);
				runningState.set(sessionId, running);

				// Notify only on a running -> stopped transition. On the
				// first observation (previous === undefined) we do not
				// notify for a past turn; otherwise every page reload would
				// raise a notification.
				if (running || previous !== true) return;
				if (!settings.onComplete) return;

				if (!settings.includeSubagents && !isMainSession(ctx, sessionId)) return;

				notify(
					sessionId,
					"Reply ready",
					sessionTitle(ctx, sessionId),
					`dsh-complete-${sessionId}`
				);
			}), "dsh-notify: turn finished");

			// --- 2) Question / approval ---------------------------------------
			ctx.effect(() => ctx.remote.$on("approval/request", (request, next) => {
				// CRITICAL: `next()` must be called after raising the
				// notification — and it must be called even when notifications are
				// switched off, otherwise the approval flow never receives an
				// answer and the agent deadlocks.
				try {
					const agent = request?.agent;
					const sessionId = agent?.id ?? "approval";
					if (settings.onQuestion
						&& (settings.includeSubagents || isMainSession(ctx, sessionId))) {
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

			// --- 3) Agent error -----------------------------------------------
			ctx.effect(() => ctx.remote.$on("api-session/error", (sessionId, error) => {
				if (!settings.onError) return;
				if (!settings.includeSubagents && !isMainSession(ctx, sessionId)) return;
				notify(
					sessionId,
					"DSH: error",
					summarize(String(error ?? "")) || sessionTitle(ctx, sessionId),
					`dsh-error-${sessionId}`
				);
			}), "dsh-notify: agent error");
		}
		//#endregion

		exports.apply = apply;
		exports.inject = inject;
		exports.name = name;
		return module.exports;
	}
});
