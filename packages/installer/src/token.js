/**
 * Token handling.
 *
 * Rule: the full token is written to exactly one place - the connection config
 * file (mode 0600) - and never to stdout, stderr, a SKILL file or a JSON report.
 * Everything that prints a token prints `maskToken(token)`.
 */

/**
 * @param {string} token
 * @returns {string} a display form that is never the token itself
 */
export function maskToken(token) {
  if (typeof token !== "string" || token.length === 0) return "(unset)";
  const prefix = token.startsWith("sr_live_") ? "sr_live_" : "";
  // Below this length, any head/tail pair would reproduce most of the secret.
  if (token.length < 20) return "****";
  const head = prefix || token.slice(0, 4);
  const tail = token.slice(-4);
  return `${head}...${tail}`;
}

/**
 * Reject obviously wrong input early: an agent token is issued by the platform as
 * `sr_live_<random>`; anything else is far more likely to be a pasted password,
 * a provider key or a truncated copy.
 *
 * This is a *warning* helper, not a hard gate: the platform is the authority on
 * whether a token works, and `verify` asks it.
 *
 * @param {string} token
 * @returns {string | null} a warning, or null when the shape looks right
 */
export function tokenShapeWarning(token) {
  if (!token) return "No token was supplied.";
  if (token.startsWith("sr_live_") && token.length >= 20) return null;
  if (/^(sk-|AIza|ghp_|xox)/.test(token)) {
    return "That looks like a provider key, not a Skill Router agent token (they start with `sr_live_`).";
  }
  return "Agent tokens normally start with `sr_live_`; continuing because the platform decides, not the CLI.";
}
