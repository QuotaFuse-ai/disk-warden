// 界面原型一键回滚入口（双击仓库根目录的 rollback-ui.bat）
// 只回滚界面原型的两个文件，回滚前自动备份当前版本，可随时撤销。
import { execFileSync, execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline/promises';

const REPO = path.resolve(import.meta.dirname, '..');
const FILES = ['ui/index.html', 'docs/specs/界面交互说明-v1.0.md'];
const BACKUP_ROOT = path.join(REPO, 'backups', 'rollback-ui');

const POINTS = [
  { sha: '9149481', name: '当前版本：浅色 + 桌面字号阶梯', note: 'v1.8 · 基准 14px / 下限 11px（推荐保留）' },
  { sha: 'd6acfd1', name: '浅色 360 改版', note: 'v1.7 · 他人改的整体样式，字号偏小' },
  { sha: 'a3b8f2e', name: '深色旧版', note: '改版前的深色三栏布局' },
];

const git = (args) => {
  try {
    return execFileSync('git', ['-C', REPO, ...args], { encoding: 'utf8' }).trim();
  } catch (e) {
    throw new Error(`git ${args[0]} 失败：${String(e.stderr || e.message).trim()}`);
  }
};

function stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

function backup() {
  const dir = path.join(BACKUP_ROOT, stamp());
  for (const rel of FILES) {
    const src = path.join(REPO, rel);
    if (!fs.existsSync(src)) continue;
    const dst = path.join(dir, rel);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
  }
  console.log(`  已备份当前版本 → ${path.relative(REPO, dir)}`);
  return dir;
}

function latestBackup() {
  if (!fs.existsSync(BACKUP_ROOT)) return null;
  const dirs = fs.readdirSync(BACKUP_ROOT).filter((d) => fs.existsSync(path.join(BACKUP_ROOT, d, 'ui', 'index.html')));
  return dirs.length ? dirs.sort().pop() : null;
}

function showDiff(diff) {
  console.log('  与 HEAD 的差异：\n    ' + diff.split('\n').join('\n    '));
}

function restore(point) {
  backup();
  // git restore 只改工作区，不动暂存区，回滚结果就是一份普通未提交改动
  git(['restore', `--source=${point.sha}`, '--', ...FILES]);
  const diff = git(['-c', 'core.quotepath=off', 'diff', '--stat', 'HEAD', '--', ...FILES]);
  if (!diff) {
    console.log('\n  文件本来就等于 ' + point.sha + '（' + point.name + '），没有变化。');
    return;
  }
  console.log(`\n  ✔ 已回滚到 ${point.sha}（${point.name}）`);
  showDiff(diff);
  console.log('\n  现在打开 ui/index.html 就是这个版本。接下来：');
  console.log('    · 满意 → 回到本入口按 4 提交固化（或先不提交，随时可撤）');
  console.log('    · 不满意 → 回到本入口按 r，立即恢复回滚前的版本');
  openPreview();
}

function undo() {
  const name = latestBackup();
  if (!name) { console.log('  没有可恢复的备份。'); return; }
  const dir = path.join(BACKUP_ROOT, name);
  for (const rel of FILES) {
    const src = path.join(dir, rel);
    if (!fs.existsSync(src)) continue;
    const dst = path.join(REPO, rel);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(src, dst);
  }
  const diff = git(['-c', 'core.quotepath=off', 'diff', '--stat', 'HEAD', '--', ...FILES]);
  console.log(`\n  ✔ 已恢复备份 ${name}`);
  if (diff) showDiff(diff);
  else console.log('    工作区已回到 HEAD 的版本。');
  openPreview();
}

function currentPoint() {
  const working = git(['hash-object', path.join(REPO, FILES[0])]);
  return POINTS.find((p) => git(['rev-parse', `${p.sha}:ui/index.html`]) === working) || null;
}

function commit() {
  const point = currentPoint();
  git(['add', '--', ...FILES]);
  if (!git(['diff', '--cached', '--name-only'])) {
    console.log('  工作区与 HEAD 一致，无需提交。');
    return;
  }
  git(['commit', '-m', `revert(ui): 界面原型回滚${point ? '到「' + point.name + '」(' + point.sha + ')' : '（自定义状态）'}`]);
  console.log('  ✔ 已提交，回滚结果进入版本库。要再退回：双击本入口按对应版本号。');
}

function openPreview() {
  if (!process.stdout.isTTY) return;
  execFile('cmd.exe', ['/c', 'start', '', path.join(REPO, 'ui', 'index.html')], () => {});
}

function menuText() {
  const lines = ['\n  Disk Warden · 界面原型回滚', '  ' + '-'.repeat(40)];
  POINTS.forEach((p, i) => lines.push(`   [${i + 1}] ${p.name}  ${p.sha}\n       ${p.note}`));
  lines.push('   [r] 撤销上一次回滚（恢复自动备份的旧文件）');
  lines.push('   [4] 提交当前回滚结果（固化为新版本）');
  lines.push('   [s] 只看当前状态，不做任何改动');
  lines.push('   [q] 退出，不做任何改动');
  return lines.join('\n');
}

function status() {
  console.log('\n  HEAD: ' + git(['log', '-1', '--format=%h %s']));
  const dirty = git(['-c', 'core.quotepath=off', 'status', '--porcelain', '--', ...FILES]);
  console.log('  未提交改动: ' + (dirty ? '\n    ' + dirty.split('\n').join('\n    ') : '无'));
  console.log('  可用备份: ' + (fs.existsSync(BACKUP_ROOT) ? fs.readdirSync(BACKUP_ROOT).join(', ') : '无'));
}

async function main() {
  const arg = (process.argv[2] || '').toLowerCase();
  if (arg === 'undo' || arg === 'r') return undo();
  if (arg === 'commit') return commit();
  if (arg && /^[0-9a-f]{4,40}$/.test(arg)) {
    const sha = git(['rev-parse', '--verify', arg + '^{commit}']);
    return restore(POINTS.find((p) => p.sha.startsWith(arg)) || { sha, name: '指定版本', note: '' });
  }
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  for (;;) {
    console.log(menuText());
    const k = (await rl.question('\n  输入选项后回车 > ')).trim().toLowerCase();
    if (k === 'q') break;
    if (k === 's') status();
    else if (k === 'r') undo();
    else if (k === '4') commit();
    else if (['1', '2', '3'].includes(k)) restore(POINTS[Number(k) - 1]);
    else console.log('  没这个选项。');
    if (['1', '2', '3', 'r', 'q'].includes(k)) break;
  }
  rl.close();
}

main().catch((e) => {
  console.error('  出错了：' + (e.message || e));
  process.exitCode = 1;
});
