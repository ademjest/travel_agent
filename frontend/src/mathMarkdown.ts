/** Normalize alternative math delimiters before Markdown consumes backslash escapes. */
function mathProse(text: string) {
  return text
    .replace(/(?<!\\)\$(\d[\d.,]*)\b(?=[ \t]*(?:[，。；,:;!?–—]|\p{Script=Han}|(?:and|to|per|each|for|USD|CNY|EUR)\b|$))/gu,
      (_match, amount: string) => "\\$" + amount)
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, body: string) => `\n\n$$\n${body.trim()}\n$$\n\n`)
    .replace(/\\\(([^\n]*?)\\\)/g, (_match, body: string) => `$${body.trim()}$`)
    .replace(/^ {0,3}\[[ \t]*\n([\s\S]*?)\n {0,3}\][ \t]*$/gm, (match, body: string) => {
      const isMath = /\\[a-zA-Z]+/.test(body) ||
        (/^[\sA-Za-z0-9=+\-*/^_{}().,|<>!]+$/.test(body) && /[=^_]/.test(body));
      return isMath ? `\n\n$$\n${body.trim()}\n$$\n\n` : match;
    });
}

function preserveInlineCode(text: string) {
  let output = "", start = 0;
  for (const match of text.matchAll(/(`+)([\s\S]*?)\1(?!`)/g)) {
    output += mathProse(text.slice(start, match.index)) + match[0];
    start = match.index! + match[0].length;
  }
  return output + mathProse(text.slice(start));
}

export function normalizeMathDelimiters(text: string) {
  let output = "", prose = "", fence = "";
  const flush = () => { output += preserveInlineCode(prose); prose = ""; };
  for (const line of text.replace(/\r\n/g, "\n").split(/(?<=\n)/)) {
    if (fence) {
      output += line;
      if (new RegExp(`^ {0,3}${fence[0]}{${fence.length},}\\s*$`).test(line)) fence = "";
      continue;
    }
    const opening = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (opening || /^(?: {4}|\t)/.test(line)) {
      flush();
      if (opening) fence = opening[1];
      output += line;
    } else prose += line;
  }
  flush();
  return output;
}
