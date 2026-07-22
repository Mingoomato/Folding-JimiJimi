import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Tailwind 클래스 병합 — 뒤에 온 클래스가 이긴다. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
