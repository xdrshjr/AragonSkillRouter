'use client';

import { PageHeader } from '@/components/ui/page-header';
import { EngineConsole } from '@/components/settings/engine-console';

export default function EngineSettingsPage() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="Engine"
        backHref="/settings"
        backLabel="Settings"
        description="The AragonAgent sidecar: which release is active, what a candidate has to pass, and what this build can actually drive."
      />
      <EngineConsole />
    </div>
  );
}
