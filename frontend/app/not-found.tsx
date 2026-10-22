import Link from 'next/link';

/**
 * Honest 404: routes owned by a later milestone (SKILLs, Access, Records,
 * Settings, the agent panel) do not exist yet. Nothing is faked.
 */
export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen max-w-content flex-col items-start justify-center gap-3 px-4">
      <p className="text-xs uppercase tracking-wide text-muted">404</p>
      <h1 className="text-xl font-semibold text-text">This page does not exist yet</h1>
      <p className="max-w-xl text-sm text-muted">
        Only Overview, Providers and Capabilities are implemented in this milestone. SKILLs, Access, Records,
        Settings and the floating agent panel are built on top of the same primitives in the next one.
      </p>
      <div className="flex gap-2">
        <Link href="/" className="rounded-control border border-border-strong bg-surface px-3 py-2 text-xs text-text hover:no-underline">
          Back to overview
        </Link>
        <Link
          href="/capabilities"
          className="rounded-control border border-transparent bg-accent px-3 py-2 text-xs font-medium text-white hover:no-underline"
        >
          Capabilities
        </Link>
      </div>
    </main>
  );
}
