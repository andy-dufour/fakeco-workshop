// Starts the mock API and Vite together; stops both on Ctrl-C.
import { spawn } from "node:child_process";

const run = (cmd, args) => spawn(cmd, args, { stdio: "inherit", shell: process.platform === "win32" });
const mock = run("node", ["mock/server.mjs"]);
const vite = run("npx", ["vite", ...process.argv.slice(2)]);
const stop = () => { mock.kill(); vite.kill(); };
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
vite.on("exit", (code) => { mock.kill(); process.exit(code ?? 0); });
mock.on("exit", (code) => { if (code) { vite.kill(); process.exit(code); } });
