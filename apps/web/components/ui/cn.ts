import clsx from 'clsx';

export type ClassValue = string | number | false | null | undefined;

/**
 * 轻量类名合并（不引入 class-variance-authority）。
 * 后出现的类不覆盖前者（Tailwind 由声明顺序决定优先级），只做拼接与去假值。
 */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
