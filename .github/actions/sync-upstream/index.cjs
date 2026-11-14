const {spawn} = require('node:child_process');
const child = spawn('python3', ['.github/scripts/sync-upstream.py'], {
  stdio: 'inherit', env: process.env
});
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal));
}
child.on('error', () => { process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code === null ? 1 : code; });
