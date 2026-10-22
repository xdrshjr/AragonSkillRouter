/**
 * Two error kinds, because they have different exit codes and different readers.
 *
 * `UsageError`  - the operator typed something wrong. exit 2.
 * `ConnectError` - the platform said no, or could not be reached. exit 1.
 *
 * Both carry `actions`: concrete next steps printed after the message. A CLI that
 * fails without saying what to do next is the thing this package exists to avoid.
 */

export class InstallerError extends Error {
  /**
   * @param {string} message
   * @param {{code?: string, actions?: string[], details?: unknown, exitCode?: number}} [options]
   */
  constructor(message, options = {}) {
    super(message);
    this.name = "InstallerError";
    this.code = options.code || "installer_error";
    this.actions = options.actions || [];
    this.details = options.details === undefined ? null : options.details;
    this.exitCode = options.exitCode || 1;
  }
}

export class UsageError extends InstallerError {
  /** @param {string} message @param {{actions?: string[]}} [options] */
  constructor(message, options = {}) {
    super(message, { code: "usage_error", exitCode: 2, ...options });
    this.name = "UsageError";
  }
}

export class ConnectError extends InstallerError {
  /**
   * @param {string} message
   * @param {{code?: string, actions?: string[], details?: unknown}} [options]
   */
  constructor(message, options = {}) {
    super(message, { code: "connect_failed", exitCode: 1, ...options });
    this.name = "ConnectError";
  }
}
