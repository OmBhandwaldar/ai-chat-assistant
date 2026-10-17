"use client";

import ReactMarkdown from "react-markdown";

/**
 * Light, safe markdown for answer text (bold, lists, line breaks, inline code, links).
 * react-markdown escapes raw HTML by default, so no dangerouslySetInnerHTML is involved.
 */
export function AnswerMarkdown({ text }: { text: string }) {
  return (
    <div className="answer-md text-sm leading-relaxed text-neutral-800 [overflow-wrap:anywhere]">
      <ReactMarkdown
        components={{
          p: (props) => <p className="my-2 whitespace-pre-line" {...props} />,
          strong: (props) => <strong className="font-semibold text-neutral-900" {...props} />,
          ul: (props) => <ul className="my-2 list-disc space-y-1 pl-5 marker:text-neutral-400" {...props} />,
          ol: (props) => <ol className="my-2 list-decimal space-y-1 pl-5 marker:text-neutral-400" {...props} />,
          code: (props) => (
            <code className="rounded bg-neutral-100 px-1 py-0.5 font-mono text-[0.85em]" {...props} />
          ),
          a: ({ href, ...props }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2 hover:text-neutral-900"
              {...props}
            />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
