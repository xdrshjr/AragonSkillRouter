# Multi-stage build for the Skill Router AragonAgent gateway (spec §8.3, §14.2).
#
# Why two stages: `typescript` lives in devDependencies, so a single-stage
# `npm ci --omit=dev && tsc` can never work (the same mistake spec §12.4-2 records for
# the engine build). The runtime image therefore contains no compiler, no test files and
# no devDependencies — only `dist/` plus the production dependency tree.

FROM node:22-slim AS build
WORKDIR /app
# NODE_ENV=production at install time would silently skip devDependencies (typescript,
# tsx, vitest) and break the build, so it is set explicitly here.
ENV NODE_ENV=development
COPY package.json package-lock.json ./
RUN npm ci --include=dev --no-audit --no-fund
COPY tsconfig.json ./
COPY src ./src
RUN npx tsc -p tsconfig.json

FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    AGENT_GATEWAY_BIND=127.0.0.1 \
    AGENT_GATEWAY_PORT=8500 \
    AGENT_ENGINE_HOME_ROOT=/app/var/aragon-home/engines \
    AGENT_POLICY_PATH=/app/var/agent-policy.json \
    AGENT_SESSION_DIR=/app/var/agent-sessions
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force
COPY --from=build /app/dist ./dist

# Non-root, with a writable var/ for `var/agent-policy.json`, per-version engine homes
# and `turns.jsonl`. The gateway never writes outside this directory.
RUN mkdir -p /app/var/aragon-home/engines /app/var/agent-sessions \
    && chown -R node:node /app
USER node

EXPOSE 8500

# /healthz is intentionally unauthenticated (no secrets in the body) so the orchestrator
# can probe it. `ok:false` means "the engine cannot serve a turn" and must fail the probe.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.AGENT_GATEWAY_PORT||8500)+'/healthz').then(r=>r.json()).then(j=>process.exit(j.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "dist/src/main.js"]
