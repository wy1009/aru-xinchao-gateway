import { createHash, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

// Match aru_portal.authorized, including passwords containing colons or UTF-8.
export function requireWebAuth(request, response, filename) {
  let expected;
  try {
    const config = readFileSync(filename, "utf8");
    const match = /^\s*INBOX_DIGEST\s*=\s*["']?([a-f0-9]{64})["']?\s*$/m.exec(config);
    if (!match) throw new Error("Missing backup login digest");
    expected = Buffer.from(match[1], "hex");
  } catch {
    response.writeHead(503, { "Cache-Control": "no-store" });
    response.end();
    return false;
  }

  const basic = /^Basic ([A-Za-z0-9+/]+=*)$/i.exec(request.headers.authorization || "");
  if (basic) {
    const credentials = Buffer.from(basic[1], "base64");
    if (credentials.toString("base64") === basic[1] && credentials.subarray(0, 4).equals(Buffer.from("aru:"))) {
      const actual = createHash("sha256").update(credentials.subarray(4)).digest();
      if (timingSafeEqual(actual, expected)) return true;
    }
  }
  response.writeHead(401, {
    "WWW-Authenticate": 'Basic realm="Aru Backup", charset="UTF-8"',
    "Cache-Control": "no-store",
  });
  response.end();
  return false;
}
