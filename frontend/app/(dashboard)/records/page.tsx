'use client';

import { useState } from 'react';
import { useSearchParams } from 'next/navigation';

import { PageHeader } from '@/components/ui/page-header';
import { TabPanel, Tabs } from '@/components/ui/tabs';
import { InvocationsTable, TasksTable } from '@/components/records/records-tables';

type TabId = 'invocations' | 'tasks';

export default function RecordsPage() {
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<TabId>(() => (searchParams.get('tab') === 'tasks' ? 'tasks' : 'invocations'));

  return (
    <div className="space-y-4">
      <PageHeader
        title="Records"
        description="Every capability call and every async task the router recorded — including the ones that failed, were rejected or are still uncertain."
      />

      <Tabs
        tabs={[
          { id: 'invocations', label: 'Invocations' },
          { id: 'tasks', label: 'Tasks' },
        ]}
        value={tab}
        onChange={(id) => setTab(id as TabId)}
        label="Record types"
        panelIdPrefix="records"
      />

      <TabPanel id="invocations" active={tab === 'invocations'} panelIdPrefix="records">
        <InvocationsTable />
      </TabPanel>
      <TabPanel id="tasks" active={tab === 'tasks'} panelIdPrefix="records">
        <TasksTable />
      </TabPanel>
    </div>
  );
}
