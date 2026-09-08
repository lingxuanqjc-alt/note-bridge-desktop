import type {Response} from './types';

export async function invoke<T>(method: string, ...args: unknown[]): Promise<T> {
  const api = window.pywebview?.api;
  if (!api?.[method]) throw new Error('请通过“笔记互迁”桌面应用使用此功能。浏览器预览不会连接你的账号。');
  const response = await api[method](...args) as Response<T>;
  if (!response?.ok) throw new Error(response?.error?.message || '操作未完成，请查看结果后重试。');
  return response.data as T;
}

