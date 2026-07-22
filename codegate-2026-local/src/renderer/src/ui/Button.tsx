import { cva, type VariantProps } from 'class-variance-authority';
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { Spinner } from './Spinner';

/**
 * Folding 액션 버튼.
 * 디자인 시스템 규칙: 한 화면에 primary는 하나. hover는 한 단계 어둡게,
 * press는 한 단계 더 + 0.5px 눌림. 바운스 없음.
 */
const button = cva(
  [
    'inline-flex items-center justify-center whitespace-nowrap select-none',
    'tracking-normal',
    'transition-[background-color,box-shadow,transform] duration-[--dur-fast] ease-[--ease-standard]',
    'active:translate-y-[0.5px]',
    'disabled:opacity-55 disabled:cursor-not-allowed disabled:active:translate-y-0',
    'fold-focus',
  ],
  {
    variants: {
      variant: {
        primary: 'bg-blue-500 text-white shadow-brand-sm hover:bg-blue-600 active:bg-blue-700',
        secondary:
          'bg-white text-ink-800 border border-ink-200 shadow-xs hover:bg-ink-50 active:bg-ink-100',
        subtle: 'bg-blue-50 text-blue-600 hover:bg-blue-100 active:bg-blue-200',
        ghost: 'bg-transparent text-ink-700 hover:bg-ink-50 active:bg-ink-100',
        danger:
          'bg-red-500 text-white shadow-[0_4px_12px_rgba(229,72,77,.22)] hover:bg-red-600 active:bg-red-600',
      },
      size: {
        sm: 'h-[34px] px-[14px] text-xs font-semibold rounded-md gap-1.5',
        md: 'h-[42px] px-5 text-[15px] font-semibold rounded-lg gap-2',
        lg: 'h-[52px] px-7 text-base font-bold rounded-lg gap-[9px]',
      },
      fullWidth: { true: 'w-full', false: '' },
    },
    defaultVariants: { variant: 'primary', size: 'md', fullWidth: false },
  },
);

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'>,
    VariantProps<typeof button> {
  children?: ReactNode;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, fullWidth, children, iconLeft, iconRight, loading, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(button({ variant, size, fullWidth }), className)}
      {...rest}
    >
      {loading ? <Spinner /> : iconLeft}
      {children}
      {!loading && iconRight}
    </button>
  );
});
