export type PlatformId = 'xiaomi' | 'oppo' | 'vivo' | 'huawei' | 'honor' | 'meizu' | 'wps';
export type Format = 'txt' | 'docx' | 'md' | 'html';
export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled' | 'needs_review';
export interface PlatformState {
  id: PlatformId; logged_in: boolean; logging_in: boolean; count: number; complete: boolean;
  capability: string; message?: string;
  login_open?: boolean;
}
export interface TaskReport {
  id: string; operation: string; status: TaskStatus; stage: string; completed: number; total: number;
  succeeded: number; skipped: number; issues: {note_id: string; code: string; message: string}[];
  output_path?: string | null; started_at: string; finished_at?: string | null;
}
export interface AppState {
  name: string; version: string; platforms: PlatformState[]; current_task: TaskReport | null;
  has_export: boolean; release_channel: 'stable' | 'preview'; release_ready: boolean;
}
export interface MigrationPreview {
  token: string; source: PlatformId; target: PlatformId; total: number;
  write_available: boolean; blocked_reason: string;
  items: {id: string; title: string; summary: string; compatible: boolean; reason: string;
    code: string; already_migrated: boolean; warnings: string[]}[];
}
export interface MigrationReview {
  source: PlatformId; target: PlatformId; cloud_checked: false;
  source_cache_complete: boolean; target_cache_complete: boolean;
  cached_notes: number; unmatched_cached_notes: number;
  items: {id: string; title: string; version: 'current' | 'previous' | 'source_missing'; status: string; remote_ids: string[];
    cached_remote_ids: string[]; resource_states: string[]}[];
}
export interface Response<T> {ok: boolean; data?: T; error?: {code: string; message: string}}
export interface NativeAPI { [key: string]: (...args: unknown[]) => Promise<Response<unknown>> }
declare global {
  interface Window {pywebview?: {api: NativeAPI}; onNoteBridgeTask?: (task: TaskReport) => void}
}
