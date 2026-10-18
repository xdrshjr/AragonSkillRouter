import { NextResponse } from 'next/server';

/**
 * 容器健康检查（docker healthcheck 与反代探活共用）。
 * 只反映 Web 进程是否可服务，不代理后端状态。
 */
export function GET() {
  return NextResponse.json({ status: 'ok' }, { status: 200, headers: { 'Cache-Control': 'no-store' } });
}

export const dynamic = 'force-static';
