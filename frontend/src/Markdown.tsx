import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { normalizeMathDelimiters } from "./mathMarkdown";

export function normalizeMarkdownText(text: string) {
  return normalizeMathDelimiters(text)
    .replace(/^\*\*([^*\n]+)\*\*[ \t]*$/gm, "## $1")
    .replace(
      /^(第\s*\d+\s*天(?:\s*·\s*[^\n]+|（日期待定）))[ \t]*$/gm,
      "\n### $1\n",
    )
    .replace(
      /^(住宿候选|餐饮候选)(?:（([^\n）]*)）)?[：:][ \t]*$/gm,
      (_match, title: string, note?: string) =>
        `\n### ${title}${note ? `（${note}）` : ""}\n`,
    )
    .replace(
      /^(旅行行程|行程创建预览（尚未保存）|行程修改预览（尚未保存）)(：[^\n]+)$/gm,
      "## $1$2",
    );
}

export function internalEvidenceLink(href?: string) {
  if (!href) return false;
  try {
    const url = new URL(href, window.location.href);
    return url.origin === window.location.origin && url.pathname === window.location.pathname &&
      /^#source-(?:[a-f0-9]{12}-)?\d+-target-\d+-(opening_hours|ticket_price|reservation_rules)$/.test(url.hash);
  } catch { return false; }
}

export function Markdown({ text }: { text: unknown }) {
  if (typeof text !== "string")
    return <p role="alert">这条内容暂时无法显示，请刷新或重新生成。</p>;
  const display = normalizeMarkdownText(text).replace(
    /^地点依据：/gm,
    "\n地点依据：",
  );
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore", maxExpand: 1000, maxSize: 20 }]]}
        skipHtml
        components={{
          a: ({ href, children }) => internalEvidenceLink(href) ? (
            null
          ) : (
            <a href={href} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          img: ({ alt }) => (
            <span className="muted">[图片链接：{alt || "请查看原始资料"}]</span>
          ),
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {display}
      </ReactMarkdown>
    </div>
  );
}

