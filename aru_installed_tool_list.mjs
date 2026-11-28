// Enumerate definitions from the installed Aru release when no operator credential is configured.
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { runInNewContext } from "node:vm";

const root = "/opt/aru-selfhost/current";
const moduleURL = (name) => pathToFileURL(join(root, name)).href;
const { createNodeWorkspaceHost } = await import(moduleURL("node-workspaces.mjs"));
const { createCollaboratorProjectHost } = await import(moduleURL("collaborator-projects.mjs"));
const { createCollaboratorSurfaceHost } = await import(moduleURL("collaborator-surfaces.mjs"));
const code = readFileSync(join(root, "aru-selfhost-stub.mjs"), "utf8");
const start = code.indexOf("function createMCPCatalog(");
const end = code.indexOf("return { createMCPCatalog };", start);
const official = /function officialMCPTools\(\)\s*\{\s*return\s*\[\s*\.\.\.MCP_TOOLS,\s*\.\.\.nodeWorkspaceHost\.tools\(\),\s*\.\.\.collaboratorHost\.surfaceTools\(\),\s*\.\.\.collaboratorHost\.projectTools\(\),?\s*\];?\s*\}/;
if (start < 0 || end < 0 || !official.test(code) ||
    !code.includes("tools: [...officialMCPTools(), ...getPluginSupervisor().dynamicMCPTools()]")) {
    throw new Error("Installed Aru catalog implementation changed");
}
const definitions = runInNewContext(code.slice(start, end) + "\ncreateMCPCatalog({})", {}, { timeout: 2000 });
const response = await fetch("http://127.0.0.1:8787/.well-known/aru.json", { redirect: "error" });
if (!response.ok) throw new Error("Aru manifest unavailable");
const { nodeKind } = await response.json();
if (!nodeKind) throw new Error("Aru manifest lacks node kind");
const dataDir = mkdtempSync(join(tmpdir(), "aru-tool-definitions-"));
try {
    const workspace = createNodeWorkspaceHost({ config: { nodeKind, managedWorkspaceRoot: join(dataDir, "work") },
        state: {}, mcpOperationTool: definitions.mcpOperationTool, mcpIdentifierSchema: definitions.mcpIdentifierSchema });
    const surfaces = createCollaboratorSurfaceHost({ dataDir });
    const projects = createCollaboratorProjectHost({ dataDir, surfaces });
    console.log(JSON.stringify([...definitions.MCP_TOOLS, ...workspace.tools(), ...surfaces.tools(), ...projects.tools()]));
} finally {
    rmSync(dataDir, { recursive: true, force: true });
}
