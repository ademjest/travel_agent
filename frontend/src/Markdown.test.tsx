// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown, normalizeMarkdownText } from "./App";

afterEach(cleanup);

describe("assistant markdown layout", () => {
  it("keeps numbered item titles with their markers and uses compact nested lists", () => {
    const text = `结论：开封特色菜适合按小吃和正餐体验。

**开封特色菜推荐**
1. **开封灌汤包**
   - 代表性很强，皮薄汤多。
   - 适合早餐或正餐。

2. **鲤鱼焙面**
   - 河南传统名菜。
   - 适合多人正餐。`;
    render(<Markdown text={text} />);
    expect(screen.getByRole("heading", { name: "开封特色菜推荐", level: 2 })).toBeTruthy();
    const outer = document.querySelector(".markdown > ol")!;
    const items = Array.from(outer.children);
    expect(items).toHaveLength(2);
    expect(items[0].firstElementChild?.textContent).toBe("开封灌汤包");
    expect(within(items[0] as HTMLElement).getAllByRole("listitem")).toHaveLength(2);
  });

  it.each([
    ["saved", "旅行行程：开封3日行程（TR-test，版本 1）"],
    ["created", "已采用方案草稿：开封参考方案。\n\n行程创建预览（尚未保存）：开封3日行程（TR-test，版本 1）"],
    ["modified", "请先核对。\n\n行程修改预览（尚未保存）：开封3日行程（TR-test，版本 2）"],
  ])("renders all day headings at one level for %s trips", (_name, prefix) => {
    const text = `${prefix}
你的要求：酒店推荐
第 1 天（日期待定）
- 下午：开封府
- 晚上：大宋武侠城
第 2 天（日期待定）
- 上午：清明上河园
- 下午：万岁山武侠城
第 3 天（日期待定）
- 上午：开封博物馆
住宿候选（地点资料，未核实实时价格和营业/库存）：
- 测试酒店`;
    render(<Markdown text={text} />);
    const headings = screen.getAllByRole("heading", { level: 3 }).map(node => node.textContent);
    expect(headings).toEqual([
      "第 1 天（日期待定）",
      "第 2 天（日期待定）",
      "第 3 天（日期待定）",
      "住宿候选（地点资料，未核实实时价格和营业/库存）",
    ]);
    const listItems = Array.from(document.querySelectorAll(".markdown li"));
    expect(listItems.some(item => item.textContent?.includes("第 2 天"))).toBe(false);
    expect(listItems.some(item => item.textContent?.includes("第 3 天"))).toBe(false);
  });

  it("normalizes dated day headings anywhere in a reply", () => {
    const normalized = normalizeMarkdownText("说明文字\n第 1 天 · 2026-10-01\n- 上午：景点");
    expect(normalized).toContain("### 第 1 天 · 2026-10-01");
  });
});

describe("assistant mathematical formulas", () => {
  it("renders the user's standalone bracket formulas with fractions and subscripts", () => {
    const text = String.raw`常用关系：
[
E=BLv,\qquad I=\frac{E}{R},\qquad F_A=BIL
]
如果磁场、导体棒和速度方向满足标准垂直关系，则：
[
F_A=\frac{B^2L^2v}{R}
]
因此安培力通常随速度增大而增大。`;
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(2);
    expect(container.querySelectorAll(".katex math mfrac").length).toBeGreaterThan(0);
    expect(container.querySelector(".katex math msub")).toBeTruthy();
    expect(screen.getByText("因此安培力通常随速度增大而增大。")).toBeTruthy();
  });

  it.each([
    [String.raw`电动势 $E=BLv$，电流 $I=\frac{E}{R}$。`, 0, 2],
    [String.raw`电动势 \(E=BLv\)，电流 \(I=\frac{E}{R}\)。`, 0, 2],
    [String.raw`\[F_A=\frac{B^2L^2v}{R}\]`, 1, 1],
    ["$$\nF_A=\\frac{B^2L^2v}{R}\n$$", 1, 1],
  ])("renders standard math delimiters: %s", (text, displayCount, totalCount) => {
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelectorAll(".katex-display")).toHaveLength(displayCount);
    expect(container.querySelectorAll(".katex")).toHaveLength(totalCount);
  });

  it("keeps fenced code, inline code and unfinished code fences literal", () => {
    const text = "```latex\n\\[E=BLv\\]\n[\nI=\\frac{E}{R}\n]\n```\n\n" +
      "`\\(E=BLv\\)` 和 ``$F_A=BIL$``\n\n" +
      "~~~latex\n\\[F_A=BIL\\]\n~~~\n\n```\n\\[I=E/R\\]";
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.querySelectorAll("pre code")).toHaveLength(3);
    expect(container.querySelector("code")?.textContent).toContain(String.raw`\[E=BLv\]`);
    expect(normalizeMarkdownText("`\\[x=1\\]`")).toBe("`\\[x=1\\]`");
  });

  it("keeps ordinary bracket text, links and currency unchanged", () => {
    const text = "[\n普通说明文字\n]\n\n[资料](https://example.com)；预算 200–300 元，票价 $20。";
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(screen.getByRole("link", { name: "资料" }).getAttribute("href")).toBe("https://example.com");
    expect(normalizeMarkdownText(text)).toContain("[\n普通说明文字\n]");
    expect(container.textContent).toContain("票价 $20。");
  });

  it("falls back for malformed LaTeX and keeps surrounding text readable", () => {
    const { container } = render(<Markdown text={String.raw`说明：$\frac{a}{$，其余内容继续显示。`} />);
    expect(container.querySelector(".katex-error")).toBeTruthy();
    expect(container.textContent).toContain("其余内容继续显示");
  });

  it.each(["酒店 $20 和餐饮 $30", "cost $20 and $30", "预算 $20–$30"])("preserves multiple currency amounts: %s", text => {
    const { container } = render(<Markdown text={text} />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.textContent).toBe(text);
  });

  it("continues rendering numeric equations and explicit math delimiters", () => {
    const { container } = render(<Markdown text={String.raw`$2 + 2 = 4$，$2xy$，\(2\text{元}\)。`} />);
    expect(container.querySelectorAll(".katex")).toHaveLength(3);
  });

  it("does not enable raw HTML or trusted LaTeX commands", () => {
    const { container } = render(<Markdown text={String.raw`<script>alert(1)</script>
$\href{javascript:alert(1)}{x}$
$\htmlClass{untrusted-formula-class}{x}$`} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(container.querySelector(".untrusted-formula-class")).toBeNull();
  });
});
