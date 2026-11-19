"use client";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Claude's replies are markdown: bold, lists, tables, code. Raw HTML in the text is never
// rendered (react-markdown's default), and links open in a new tab.
const components: Components = {
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5 first:mt-0 last:mb-0">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5 first:mt-0 last:mb-0">{children}</ol>,
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  h1: ({ children }) => <h3 className="mb-1 mt-3 text-[15px] font-semibold first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-1 mt-3 text-[15px] font-semibold first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-1 mt-3 font-semibold first:mt-0">{children}</h4>,
  h4: ({ children }) => <h4 className="mb-1 mt-3 font-semibold first:mt-0">{children}</h4>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-[var(--cs-border)] pl-3 text-[var(--cs-muted)]">{children}</blockquote>
  ),
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-[var(--cs-link)] underline underline-offset-2">
      {children}
    </a>
  ),
  code: ({ className, children }) =>
    className ? (
      <code className={className}>{children}</code>
    ) : (
      <code className="rounded bg-[var(--cs-surface-2)] px-1 py-0.5 font-mono text-[13px]">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="cs-scroll my-2 overflow-x-auto rounded-lg bg-[var(--cs-surface-2)] p-3 font-mono text-[13px] leading-5">{children}</pre>
  ),
  table: ({ children }) => (
    <div className="cs-scroll my-2 overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border-b border-[var(--cs-border)] px-2 py-1 text-left font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-b border-[var(--cs-border)] px-2 py-1 align-top">{children}</td>,
  hr: () => <hr className="my-3 border-[var(--cs-border)]" />,
};

/** Claude's text, as markdown. */
export function Markdown({ text }: { text: string }) {
  return (
    <div data-testid="markdown" className="min-w-0 [overflow-wrap:anywhere]">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
