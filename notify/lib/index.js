/**
 * dsh-notify — Host half.
 *
 * This plugin has no model-facing surface and registers no tools or prompts.
 * Its only job is to host the user settings (which events should notify) under
 * the `notify` namespace and to publish the defaults the client half reads.
 *
 * The notification itself is produced in the client (browser): QtWebEngine and
 * Chrome bridge `new Notification(...)` to the operating system's notification
 * service. That path removes the `notify-send` plus session-bus problem
 * entirely, and it also makes "click to focus the window" possible.
 *
 * Why a host half exists at all: the settings must be stored in DSH's own
 * settings document (user-settings). Browser localStorage is tied to the
 * profile directory, and `dsh-app-window.sh` and `dsh-tray.py` use different
 * profiles; a host-side setting applies to both.
 *
 * @module dsh-notify
 */

import z from "@deepseek-ai/schemastery";

/** Cordis plugin name. */
export const name = "dsh-notify";

/**
 * The host half has no service dependency. Settings are used when present and
 * the defaults apply otherwise, so `inject` is left empty and the settings
 * registration is optional.
 */
export const inject = [];

/** Settings namespace owned by this plugin. */
export const SETTINGS_NAMESPACE = "notify";

/** Default settings; the client half uses the same values as a fallback. */
export const DEFAULTS = Object.freeze({
  onComplete: true,
  onQuestion: true,
  onError: true,
  onlyWhenHidden: true,
  includeSubagents: false,
  sound: false,
});

/**
 * Plugin configuration.
 *
 * Note: `volatile()` is NOT used. In schemastery a `.volatile()` chain destroys
 * the `.default(...)` value (`z.boolean().default(true).volatile()` yields `{}`
 * rather than `true`). That made `settings.configure()` run against a broken
 * schema and the plugin never activated ("failed to import" /
 * "1 entry did not activate").
 *
 * The fields set on reply completion or turn start do not need to be live;
 * defaults plus reading the stored settings are enough.
 */
export const Config = z.object({
  /** Notify when the assistant finishes a reply. */
  onComplete: z.boolean().default(DEFAULTS.onComplete),
  /** Notify when the agent asks a question or waits for approval. */
  onQuestion: z.boolean().default(DEFAULTS.onQuestion),
  /** Notify when the agent ends with an error. */
  onError: z.boolean().default(DEFAULTS.onError),
  /** Notify only while the window is not focused (to avoid interrupting). */
  onlyWhenHidden: z.boolean().default(DEFAULTS.onlyWhenHidden),
  /** Also notify for subagent sessions. */
  includeSubagents: z.boolean().default(DEFAULTS.includeSubagents),
  /** Play the notification sound. */
  sound: z.boolean().default(DEFAULTS.sound),
});


/**
 * Attach the plugin.
 *
 * Registers the settings namespace when the `settings` service is present. If
 * registration fails (for example when `settings` is absent) the plugin keeps
 * working: the client half falls back to the defaults above when it cannot read
 * the configuration.
 *
 * @param ctx - Host plugin context.
 */
export function apply(ctx) {
  ctx.inject(["settings"], (child) => {
    // `auto: false` — this plugin declares its own settings schema via Config.
    child.effect(() => child.settings.configure({ auto: false }, ctx.fiber));
  });
}
