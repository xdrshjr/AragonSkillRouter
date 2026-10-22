/**
 * Entry point.
 *
 * Boot order matters: configuration first (it refuses to start without a bearer
 * token or with a non-loopback host), then the listener, then a readiness line
 * the platform can scrape. Any failure exits non-zero with the reason on stderr —
 * a half-started sidecar that answers /health while being unable to run an agent
 * is worse than one that is plainly down.
 */

import { loadConfig } from "./config.js";
import { createSidecarServer } from "./server.js";

async function main(): Promise<void> {
  const config = loadConfig();
  const sidecar = createSidecarServer(config);

  await new Promise<void>((resolve, reject) => {
    sidecar.server.once("error", reject);
    sidecar.server.listen(config.port, config.host, () => {
      sidecar.server.off("error", reject);
      resolve();
    });
  });

  const address = sidecar.server.address();
  const port = typeof address === "object" && address !== null ? address.port : config.port;
  // The token is deliberately absent from this line.
  console.log(
    `[sidecar] listening on http://${config.host}:${port} ` +
      `engine=${config.enginePackage}@${config.engineVersion} ` +
      `channel=${config.channel} adapter=${"1.0.0"}`,
  );

  const shutdown = async (signal: string): Promise<void> => {
    console.log(`[sidecar] ${signal} received, disposing sessions`);
    try {
      await sidecar.close();
    } finally {
      process.exit(0);
    }
  };
  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
}

main().catch((error: unknown) => {
  console.error(`[sidecar] fatal: ${error instanceof Error ? error.message : String(error)}`);
  process.exit(2);
});
