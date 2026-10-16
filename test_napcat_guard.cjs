// Evaluate only the injected guard, with no QQ import, socket or real destination.
const fs = require('fs');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, 'vendor/napcat/napcat/napcat.mjs'), 'utf8').replace(/\r\n/g, '\n');
const prefix = '  async sendMsg(e, n, r = 1e4) {\n';
const start = source.indexOf(prefix) + prefix.length;
if (start < prefix.length || source.indexOf(prefix, start) !== -1) throw new Error('Unexpected build');
const end = source.indexOf('    if (e.chatType ===', start);
const guardSource = source.slice(start, end);
if (!guardSource.includes('QBOT_FIXED_GROUP_GUARD')) throw new Error('Missing guard');
const guard = new Function('e', 'Te', guardSource);
const types = { KCHATTYPEGROUP: 2 };
guard({chatType: 2, peerUid: '1050045834'}, types);
guard({chatType: 2, peerUid: '312579123'}, types);
guard({chatType: 2, peerUid: '1006808801'}, types);
guard({chatType: 2, peerUid: '929618496'}, types);
for (const peer of [
  {chatType: 1, peerUid: '1050045834'},
  {chatType: 1, peerUid: '312579123'},
  {chatType: 1, peerUid: '1006808801'},
  {chatType: 1, peerUid: '929618496'},
  {chatType: 2, peerUid: '312579124'},
  {chatType: 2, peerUid: '1050045835'},
  {chatType: 2, peerUid: '1006808802'},
  {chatType: 2, peerUid: '929618497'},
  {chatType: 2, peerUid: 'NOT-A-REAL-GROUP'},
  {chatType: 100, peerUid: '1050045834'},
  {chatType: 2},
  {},
]) {
  let blocked = false;
  try { guard(peer, types); } catch { blocked = true; }
  if (!blocked) throw new Error('Guard allowed an unauthorized destination');
}
console.log('NapCat guard verified offline: four allowed groups, all other test routes rejected.');
