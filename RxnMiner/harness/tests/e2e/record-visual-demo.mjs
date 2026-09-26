#!/usr/bin/env node

/** Record the single English visual workspace walkthrough from the sealed replay. */
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);
const output = path.resolve(process.env.RXNMINER_DEMO_OUTPUT || 'website/public/media');
const workspaceUrl = process.env.RXNMINER_WORKSPACE_URL;
if (!workspaceUrl) throw new Error('RXNMINER_WORKSPACE_URL is required');
await mkdir(output, { recursive: true });
const scratch = path.join(output, '.visual-recording');
await rm(scratch, { recursive: true, force: true });
await mkdir(scratch, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, colorScheme: 'light', locale: 'en-US', recordVideo: { dir: scratch, size: { width: 1920, height: 1080 } } });
const recordingStart = Date.now();
const page = await context.newPage();
const errors = [];
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
await page.goto(workspaceUrl, { waitUntil: 'networkidle' });
await page.waitForTimeout(3500);
// Keep the conversation visible; only anonymize its implementation-specific label.
await page.evaluate(() => {
  const anonymize = () => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.parentElement?.closest('script,style')) continue;
      if (/pi\s+agent/i.test(node.textContent || '')) {
        node.textContent = (node.textContent || '').replace(/pi\s+agent/gi, 'Agent');
      }
      if (node.textContent?.trim() === 'π' && node.parentElement?.closest('#agentpane')) {
        node.textContent = 'R';
      }
    }
  };
  anonymize();
  new MutationObserver(anonymize).observe(document.body, { childList: true, subtree: true, characterData: true });
});
if (!await page.locator('#agentpane').isVisible()) throw new Error('The Agent conversation pane must stay visible');
if (/pi\s+agent/i.test(await page.locator('#agentpane').innerText())) throw new Error('Agent label was not anonymized');
await page.addStyleTag({ content: `
  #rxnminer-demo-caption{position:fixed;z-index:2147483647;left:42px;right:42px;bottom:30px;display:grid;gap:5px;padding:15px 20px;border:1px solid #91acd1;border-radius:10px;background:rgba(20,35,59,.94);color:#f7fbff;box-shadow:0 10px 35px rgba(13,28,52,.28);font:16px/1.4 Inter,"Segoe UI",sans-serif}
  #rxnminer-demo-caption strong{color:#b9c9ff;font-size:13px;letter-spacing:.08em;text-transform:uppercase}
` });
const cues = [
  ['00:00:00.000','00:00:05.000','Sealed visual replay','A review-ready extraction opens with source evidence, provenance, and structured chemistry in one workspace.'],
  ['00:00:05.000','00:00:09.000','Source evidence','Detection boxes remain anchored to the uploaded figure so every reaction can be traced back to its source.'],
  ['00:00:09.000','00:00:13.000','Source evidence','The box overlay can be hidden and restored without changing the immutable source image.'],
  ['00:00:13.000','00:00:18.000','Recognition evidence','The original wire string, raw MolParser values, score, and digest stay inspectable beside the structure preview.'],
  ['00:00:18.000','00:00:23.000','Reaction review','Template conditions, molecule roles, and reaction examples are presented as reviewable V2 records.'],
  ['00:00:23.000','00:00:28.000','Reaction review','Raw evidence and derived chemistry remain distinct while the result stays easy to inspect.'],
  ['00:00:28.000','00:00:33.000','Export-ready output','JSON, CSV, RXN, and extraction report exports are available from the same review surface.'],
  ['00:00:33.000','00:00:37.000','English visual workspace','This recording uses a sealed replay and makes no new model or MolParser call.'],
];
const narration = process.env.RXNMINER_NARRATION ? JSON.parse(await readFile(process.env.RXNMINER_NARRATION, 'utf8')) : null;
const contentStart = (Date.now() - recordingStart) / 1000;
const timestamp = seconds => new Date(seconds * 1000).toISOString().slice(11, 23);
const secondsAt = value => value.split(':').reduce((total, part) => total * 60 + Number(part), 0);
for (const [index, cue] of cues.entries()) {
  const [, , title, body] = cue;
  const holdMs = narration ? (narration[index].duration + 0.5) * 1000 : (secondsAt(cue[1]) - secondsAt(cue[0])) * 1000;
  cue[0] = timestamp((Date.now() - recordingStart) / 1000 - contentStart);
  await page.evaluate(({ title, body }) => {
    let el = document.querySelector('#rxnminer-demo-caption');
    if (!el) { el = document.createElement('div'); el.id = 'rxnminer-demo-caption'; document.body.append(el); }
    el.innerHTML = `<strong>${title}</strong><span>${body}</span>`;
  }, { title, body });
  if (title === 'Source evidence' && body.startsWith('Detection')) {
    await page.getByRole('button', { name: /BBox×27/ }).click();
  } else if (title === 'Source evidence') {
    await page.getByRole('button', { name: /BBox×27/ }).click();
    await page.getByRole('button', { name: 'p0', exact: true }).click();
  } else if (title === 'Recognition evidence') {
    await page.getByRole('button', { name: 'MP original', exact: true }).first().click();
  } else if (title === 'Reaction review') {
    if (body.startsWith('Template')) {
      await page.getByRole('button', { name: 'MP original', exact: true }).first().click();
      await page.locator('#reaction-example-0').scrollIntoViewIfNeeded();
    } else {
      await page.locator('#reaction-example-0').getByRole('button', { name: '▾', exact: true }).first().click();
    }
  } else if (title === 'Export-ready output') {
    await page.getByText('Export', { exact: true }).click();
  } else if (title === 'English visual workspace') {
    await page.keyboard.press('Escape');
  }
  await page.waitForTimeout(holdMs);
  if (narration) cue[3] = narration[index].text;
  cue[1] = timestamp((Date.now() - recordingStart) / 1000 - contentStart);
}
const video = page.video();
await context.close();
await browser.close();
const recorded = await video.path();
const webm = path.join(output, 'rxnminer-visual-demo.webm');
await rename(recorded, webm);
const final = path.join(output, 'rxnminer-visual-demo.mp4');
const ffmpeg = process.env.FFMPEG_PATH || 'ffmpeg';
const { stdout: probe } = await execFileAsync(process.env.FFPROBE_PATH || 'ffprobe', ['-v','error','-show_entries','format=duration','-of','csv=p=0',webm]);
const trimStart = Math.max(0, Number(probe.trim()) - secondsAt(cues.at(-1)[1]));
const inputs = narration ? narration.flatMap(segment => ['-i', path.resolve(segment.file)]) : [];
const filters = narration ? narration.map((_, i) => `[${i+1}:a]adelay=${Math.round(secondsAt(cues[i][0])*1000)}:all=1[a${i}]`).concat(`${narration.map((_,i)=>`[a${i}]`).join('')}amix=inputs=${narration.length}:normalize=0[audio]`).join(';') : '';
await execFileAsync(ffmpeg, ['-y', '-ss', String(trimStart), '-i', webm, ...inputs, ...(narration ? ['-filter_complex', filters, '-map','0:v','-map','[audio]','-c:a','aac','-b:a','160k'] : ['-an']), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-t',String(secondsAt(cues.at(-1)[1])), final]);
await execFileAsync(ffmpeg, ['-y','-ss','2','-i',final,'-frames:v','1','-update','1','-vf','scale=1280:-1',path.join(output,'rxnminer-visual-demo-poster.jpg')]);
await rm(webm, { force: true });
const vtt = `WEBVTT\n\n${cues.map(([a,b,t,d],i)=>`${i+1}\n${a} --> ${b}\n${t}\n${d}`).join('\n\n')}\n`;
const transcript = cues.map(([a,b,t,d])=>`[${a} - ${b}] ${t}\n${d}`).join('\n\n')+'\n';
await writeFile(path.join(output,'rxnminer-visual-demo.vtt'),vtt);
await writeFile(path.join(output,'rxnminer-visual-demo.txt'),transcript);
const digest = async p => createHash('sha256').update(await readFile(p)).digest('hex');
const duration = timestamp(Math.ceil(secondsAt(cues.at(-1)[1]))).slice(3,8);
const manifest = { schema:'rxnminer.site-demo-media.v3', repository:'rxnminer/RxnMiner', release:'rxnminer-visual-demo-v3', privateRelease:true, source:'sealed-replay', duration, browserQa:{ errors }, assets:[] };
for (const file of ['rxnminer-visual-demo.mp4','rxnminer-visual-demo.vtt','rxnminer-visual-demo.txt','rxnminer-visual-demo-poster.jpg']) manifest.assets.push({ file, sha256: await digest(path.join(output,file)) });
await writeFile(path.join(output,'..','..','demo-media.json'), JSON.stringify(manifest,null,2)+'\n');
await rm(scratch, { recursive:true, force:true });
console.log(JSON.stringify({ ...manifest, video: final }));
