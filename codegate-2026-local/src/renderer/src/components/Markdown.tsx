import { Fragment } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { splitPages } from '@/lib/pages';

function PageDivider({ page }: { page: number }) {
  return (
    <div className="my-4 flex items-center gap-2.5" aria-label={`${page}쪽`}>
      <span className="h-px flex-1 bg-ink-100" />
      <span className="shrink-0 font-mono text-2xs text-ink-300">{page}쪽</span>
      <span className="h-px flex-1 bg-ink-100" />
    </div>
  );
}

/**
 * 답변 본문 마크다운 렌더러.
 *
 * 위키 원문은 표·제목·목록이 섞인 마크다운이다. 예전에는 `whitespace-pre-wrap` 으로
 * 날것 그대로 뿌려서 `| --- | --- |` 같은 표 구분선이 그대로 보였다. 특히 스캔 문서를
 * OCR 한 결과는 표가 많아 읽기가 더 나빴다.
 *
 * 스타일은 각 요소에 직접 준다 — typography 플러그인을 새로 들이는 것보다,
 * 이 앱의 토큰(ink/blue 스케일)에 맞추는 편이 화면과 어긋나지 않는다.
 *
 * `remark-gfm` 은 표·취소선·자동링크를 위해 넣었다. **HTML 은 렌더링하지 않는다** —
 * 문서에서 온 내용을 그대로 실행하지 않기 위해서다(react-markdown 기본값이 그렇다).
 */
export function Markdown({ children }: { children: string }) {
  const segments = splitPages(children);
  return (
    <>
      {segments.map((segment, index) => (
        <Fragment key={index}>
          {segment.page !== null && <PageDivider page={segment.page} />}
          <MarkdownBody>{segment.text}</MarkdownBody>
        </Fragment>
      ))}
    </>
  );
}

function MarkdownBody({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
        h1: ({ children }) => (
          <h1 className="mb-2 mt-4 text-[17px] font-bold text-ink-950 first:mt-0">{children}</h1>
        ),
        h2: ({ children }) => (
          <h2 className="mb-2 mt-4 text-[16px] font-bold text-ink-950 first:mt-0">{children}</h2>
        ),
        h3: ({ children }) => (
          <h3 className="mb-1.5 mt-3 text-[15px] font-semibold text-ink-900 first:mt-0">
            {children}
          </h3>
        ),
        h4: ({ children }) => (
          <h4 className="mb-1.5 mt-3 text-[15px] font-semibold text-ink-700 first:mt-0">
            {children}
          </h4>
        ),
        ul: ({ children }) => <ul className="mb-3 list-disc pl-5 last:mb-0">{children}</ul>,
        ol: ({ children }) => <ol className="mb-3 list-decimal pl-5 last:mb-0">{children}</ol>,
        li: ({ children }) => <li className="mb-1 last:mb-0">{children}</li>,
        strong: ({ children }) => <strong className="font-semibold text-ink-950">{children}</strong>,
        em: ({ children }) => <em className="italic">{children}</em>,
        blockquote: ({ children }) => (
          <blockquote className="mb-3 border-l-2 border-ink-150 pl-3 text-ink-600 last:mb-0">
            {children}
          </blockquote>
        ),
        hr: () => <hr className="my-4 border-ink-100" />,
        a: ({ children, href }) => (
          // 외부 링크는 메인이 기본 브라우저로 연다 (setWindowOpenHandler).
          <a href={href} target="_blank" rel="noreferrer" className="text-blue-600 underline">
            {children}
          </a>
        ),
        code: ({ className, children }) => {
          // 언어가 붙은 블록만 코드블록으로 본다. 인라인은 배경만 준다.
          const block = /language-/.test(className ?? '');
          return block ? (
            <code className="block font-mono text-[13px] leading-relaxed">{children}</code>
          ) : (
            <code className="rounded-xs bg-ink-100 px-1 py-0.5 font-mono text-[13px] text-ink-800">
              {children}
            </code>
          );
        },
        pre: ({ children }) => (
          <pre className="mb-3 overflow-x-auto rounded-md bg-ink-50 p-3 last:mb-0">{children}</pre>
        ),
        // 표는 본문보다 넓을 수 있다. 페이지가 아니라 **표만** 가로로 스크롤되게 감싼다.
        table: ({ children }) => (
          <div className="mb-3 overflow-x-auto last:mb-0">
            <table className="w-full border-collapse text-[13px]">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead className="bg-ink-50">{children}</thead>,
        th: ({ children }) => (
          <th className="border border-ink-100 px-2.5 py-1.5 text-left font-semibold text-ink-800">
            {children}
          </th>
        ),
        td: ({ children }) => (
          <td className="border border-ink-100 px-2.5 py-1.5 align-top text-ink-700">{children}</td>
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  );
}
