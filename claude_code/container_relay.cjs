// This file is mounted read-only by the host, outside the candidate workspace.
'use strict';
const http = require('node:http');
const fs = require('node:fs');
const {spawn} = require('node:child_process');

const server = http.createServer((req, res) => {
  const upstream = http.request({socketPath: '/run/nl2repo-model/api.sock',
    path: req.url, method: req.method, headers: req.headers}, response => {
    res.writeHead(response.statusCode, response.headers);
    response.pipe(res);
  });
  upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
  res.on('close', () => upstream.destroy());
  req.pipe(upstream);
});
server.requestTimeout = 0;
server.listen(18080, '127.0.0.1', () => {
  fs.mkdirSync(process.env.HOME, {recursive: true});
  const child = spawn('claude', process.argv.slice(2), {stdio: 'inherit'});
  child.on('error', () => process.exit(127));
  child.on('exit', (code, signal) => { server.close(); process.exit(code ?? 1); });
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
});
