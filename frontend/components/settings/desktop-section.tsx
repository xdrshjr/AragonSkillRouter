'use client';

/**
 * Desktop-only settings section.
 *
 * Only rendered inside the Electron shell (`isDesktop()`). It reports the local
 * process facts the web build has no concept of — which ports the API and engine
 * listen on, where the database lives, whether the bundled agent engine started —
 * and offers the three actions that belong to the shell rather than the API:
 * open the data folder, open the logs folder, restart the local API.
 *
 * Nothing here is a claim the shell cannot verify: every value comes from an IPC
 * call that queries the running processes, and a failed call renders an explicit
 * error rather than a stale number.
 */

import { useCallback, useEffect, useState } from 'react';
import { FolderOpen, HardDrive, RefreshCw, Server, Terminal } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { InlineAlert } from '@/components/ui/banner';
import { Button } from '@/components/ui/button';
import { Card, CardBody, CardHeader } from '@/components/ui/card';
import { Switch } from '@/components/ui/checkbox';
import { SectionHeading } from '@/components/ui/page-header';
import { Skeleton } from '@/components/ui/skeleton';
import { useToast } from '@/components/ui/toast';
import {
  describeHealth,
  desktopBridge,
  isDesktop,
  type DesktopHealth,
  type DesktopInfo,
  type DesktopPreferences,
} from '@/lib/desktop';

export function DesktopSection() {
  const [mounted, setMounted] = useState(false);
  const [info, setInfo] = useState<DesktopInfo | null>(null);
  const [health, setHealth] = useState<DesktopHealth | null>(null);
  const [prefs, setPrefs] = useState<DesktopPreferences | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();

  // The bridge only exists in the renderer, so the first paint must match the
  // server-rendered HTML; decide after mount.
  useEffect(() => {
    setMounted(true);
  }, []);

  const refresh = useCallback(async () => {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      const [nextInfo, nextHealth, nextPrefs] = await Promise.all([
        bridge.info(),
        bridge.health(),
        bridge.getSettings(),
      ]);
      setInfo(nextInfo);
      setHealth(nextHealth);
      setPrefs(nextPrefs);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    if (!mounted || !isDesktop()) return;
    void refresh();
    const bridge = desktopBridge();
    if (!bridge) return;
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [mounted, refresh]);

  if (!mounted || !isDesktop()) return null;

  const run = async (label: string, action: () => Promise<unknown>): Promise<void> => {
    setBusy(label);
    try {
      await action();
      await refresh();
      toast.success({ title: `${label} done` });
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      toast.error({ title: `${label} failed`, description: message });
      setError(message);
    } finally {
      setBusy(null);
    }
  };

  const update = async (patch: Partial<DesktopPreferences>, label: string): Promise<void> => {
    const bridge = desktopBridge();
    if (!bridge) return;
    setBusy(label);
    try {
      const next = await bridge.setSettings(patch);
      setPrefs(next);
    } catch (cause) {
      toast.error({
        title: 'Preference not saved',
        description: cause instanceof Error ? cause.message : String(cause),
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section aria-labelledby="desktop-heading" className="space-y-3">
      <SectionHeading
        title="Desktop app"
        description="This copy of Skill Router runs its API, interface and agent engine on this machine."
      />
      <Card>
        <CardHeader
          title="Local services"
          description={describeHealth(health)}
          actions={
            <Button
              size="sm"
              variant="secondary"
              iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
              loading={busy === 'Refresh'}
              onClick={() => void run('Refresh', refresh)}
            >
              Refresh
            </Button>
          }
        />
        <CardBody className="space-y-4">
          {error ? (
            <InlineAlert tone="danger">
              Could not read the desktop status: {error}. Use “Open logs folder” to see why.
            </InlineAlert>
          ) : null}

          {!info || !health ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-56" />
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-4 w-64" />
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <Fact
                  icon={<Server aria-hidden className="h-3.5 w-3.5" />}
                  label="API"
                  value={health.backendReady ? `running · port ${health.backendPort}` : 'not responding'}
                  tone={health.backendReady ? 'success' : 'danger'}
                />
                <Fact
                  icon={<Terminal aria-hidden className="h-3.5 w-3.5" />}
                  label="Interface server"
                  value={health.webReady ? `running · port ${health.webPort}` : 'not responding'}
                  tone={health.webReady ? 'success' : 'danger'}
                />
                <Fact
                  icon={<HardDrive aria-hidden className="h-3.5 w-3.5" />}
                  label="Agent engine"
                  value={
                    health.sidecarReady
                      ? `${info.engineVersion} · port ${health.sidecarPort}`
                      : 'not running — capabilities unaffected'
                  }
                  tone={health.sidecarReady ? 'success' : 'warning'}
                />
              </div>

              <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                <Row label="App version" value={info.appVersion} />
                <Row label="Adapter version" value={info.adapterVersion} />
                <Row label="Engine package" value={info.enginePackage} />
                <Row label="Platform" value={`${info.platform} ${info.arch}`} />
                <Row label="Data folder" value={info.dataDir} mono />
                <Row label="Logs folder" value={info.logsDir} mono />
              </dl>

              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="secondary"
                  iconLeft={<FolderOpen aria-hidden className="h-3.5 w-3.5" />}
                  onClick={() => void run('Open data folder', () => desktopBridge()!.openDataFolder())}
                >
                  Open data folder
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  iconLeft={<Terminal aria-hidden className="h-3.5 w-3.5" />}
                  onClick={() => void run('Open logs folder', () => desktopBridge()!.openLogsFolder())}
                >
                  Open logs folder
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  iconLeft={<RefreshCw aria-hidden className="h-3.5 w-3.5" />}
                  loading={busy === 'Restart local API'}
                  onClick={() => void run('Restart local API', () => desktopBridge()!.restartBackend())}
                >
                  Restart local API
                </Button>
              </div>
            </>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Startup and window"
          description="Stored on this machine only; they do not sync anywhere."
        />
        <CardBody className="space-y-4">
          <Switch
            label="Keep running in the tray when the window is closed"
            hint="The API keeps serving external agents while the window is hidden."
            checked={prefs?.closeToTray ?? true}
            disabled={busy !== null}
            onCheckedChange={(checked) => void update({ closeToTray: checked }, 'Tray preference')}
          />
          <Switch
            label="Start Skill Router when I sign in"
            hint="Registers the app with the operating system's login items."
            checked={prefs?.launchAtLogin ?? false}
            disabled={busy !== null}
            onCheckedChange={(checked) => void update({ launchAtLogin: checked }, 'Startup preference')}
          />
          <Switch
            label="Start the agent engine with the app"
            hint="Turning this off saves memory. Published capabilities keep working; agent sessions and engine-backed SKILL steps become unavailable."
            checked={prefs?.startAgentEngine ?? true}
            disabled={busy !== null}
            onCheckedChange={(checked) => void update({ startAgentEngine: checked }, 'Engine preference')}
          />
        </CardBody>
      </Card>
    </section>
  );
}

function Fact({
  icon,
  label,
  value,
  tone,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  tone: 'success' | 'danger' | 'warning';
}) {
  return (
    <div className="rounded-control border border-border bg-surface-muted px-3 py-2">
      <span className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-muted">
        {icon}
        {label}
      </span>
      <span className="mt-1 block">
        <Badge tone={tone}>{value}</Badge>
      </span>
    </div>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex min-w-0 items-baseline justify-between gap-3 border-b border-border/60 pb-0.5">
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd className={`truncate text-right ${mono ? 'font-mono text-xs' : ''}`} title={value}>
        {value}
      </dd>
    </div>
  );
}
