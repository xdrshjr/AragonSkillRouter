'use client';

import { useEffect, useState } from 'react';
import { Info, Terminal } from 'lucide-react';

import { CodeBlock } from '@/components/ui/code-block';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { InlineAlert } from '@/components/ui/banner';

/**
 * The install block for `/access`.
 *
 * What the backend actually exposes today:
 *  - `POST /api/admin/agents/{id}/tokens` returns the token (once) and no command;
 *  - `GET /api/v1/skill` returns the ready-made connection SKILL, but it is behind
 *    `require_agent` (bearer token), so a browser session cannot fetch it;
 *  - `packages/installer` is not part of this checkout, so there is no published
 *    `skill-router-connect` package either.
 *
 * The block therefore shows the documented command with placeholders, the real
 * verify call, and says plainly which part is not available yet instead of
 * pretending a package exists.
 */
export function InstallSection({ tokenPrefix }: { tokenPrefix?: string | null }) {
  const [origin, setOrigin] = useState('http://localhost:8000');

  useEffect(() => {
    if (typeof window !== 'undefined') setOrigin(window.location.origin);
  }, []);

  const apiBase = `${origin}/api/v1`;
  const install = `npx skill-router-connect install --url ${apiBase} --token <your-agent-token>`;
  const verify = `curl -s ${apiBase}/whoami -H "Authorization: Bearer $SR_TOKEN"`;
  const skillFetch = `curl -s ${apiBase}/skill -H "Authorization: Bearer $SR_TOKEN"`;
  const tarball = [
    'cd packages/installer',
    'npm pack',
    `npx ./skill-router-connect-$(node -p "require('./package.json').version").tgz install --url ${apiBase} --token $SR_TOKEN`,
  ].join('\n');

  return (
    <Card>
      <CardHeader
        title="Connect an agent"
        description="Give the agent the platform URL and its own token. It never receives provider credentials."
        actions={
          tokenPrefix ? (
            <span className="font-mono text-[11px] text-muted">last prefix {tokenPrefix}…</span>
          ) : null
        }
      />
      <CardBody className="space-y-3">
        <InlineAlert tone="info">
          Replace <code>&lt;your-agent-token&gt;</code> with the token shown when you created it. The platform only stores
          a hash, so this page cannot fill it in for you.
        </InlineAlert>

        <CodeBlock
          code={install}
          label="install (npm installer CLI, CONTRACT §11)"
          notice="The installer writes a SKILL file (or connection config) for the target agent and stores the URL + token in the agent's own configuration."
        />

        <CodeBlock
          code={verify}
          label="verify connectivity"
          notice="GET /api/v1/whoami returns {agent, scopes, limits, capabilities_count} — the same call the installer uses."
        />

        <CodeBlock
          code={skillFetch}
          label="fetch the ready-made connection SKILL"
          notice="GET /api/v1/skill is bearer-only, so it must be fetched by the agent (or curl), not by this admin page."
        />

        <div className="rounded-card border border-border bg-info-soft p-3 text-xs text-text">
          <p className="flex items-center gap-1.5 font-medium">
            <Info aria-hidden className="h-3.5 w-3.5" /> What is not available in this build
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 text-text/80">
            <li>
              <code>skill-router-connect</code> is not published, and <code>packages/installer</code> is absent from this
              checkout — the <code>npx</code> line above is the documented flow (CONTRACT §11), not a verified package.
            </li>
            <li>
              No admin endpoint returns a pre-built install command: the token response carries{' '}
              <code>token</code>, <code>warning</code> and <code>token_record</code> only.
            </li>
          </ul>
          <div className="mt-2">
            <CodeBlock code={tarball} label="local tarball flow (once the installer exists)" maxHeightClassName="max-h-32" />
          </div>
        </div>

        <p className="flex items-center gap-1.5 text-[11px] text-muted">
          <Terminal aria-hidden className="h-3 w-3" />
          MCP clients can use {origin}/api/mcp with the same bearer token (streamable HTTP).
        </p>
      </CardBody>
    </Card>
  );
}
