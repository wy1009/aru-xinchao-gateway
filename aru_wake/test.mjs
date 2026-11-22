import assert from 'node:assert/strict';
import {mkdtemp,rm,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomBytes,randomUUID,createDecipheriv} from 'node:crypto';
import {deliver} from './adapter.mjs';
process.env.ARU_PUBLIC_ORIGIN = 'https://gateway.example.org';
const spool=await mkdtemp(join(tmpdir(),'aru-wake-test-'));
try {
 const key=randomBytes(32), id=randomUUID();
 const bundle={schema:'aru.wake-bridge.sender-bundle.v2',submitURL:'https://gateway.example.org/aru/v1/wake-bridge/endpoints/e/events',submitToken:'synthetic',triggerId:'synthetic',encryptionKey:key.toString('base64')};
 const note={reason:'user_note',deliveryId:'delivery-'+id,protocol:'xinchao-runtime-wake/1',message:'test only'};
 let state='missing';const packets=[];
 const opts={spool,delay:async()=>{},receipt:async()=>state,fetch:async (url,r)=>{
   assert.equal(url,'http://127.0.0.1:8787/aru/v1/wake-bridge/endpoints/e/events');
   const p=JSON.parse(r.body);packets.push(r.body);return {status:202,json:async()=>({accepted:true,eventId:p.eventId})};}};
 await assert.rejects(deliver(note,bundle,opts),/waiting for phone/);
 await assert.rejects(deliver(note,bundle,opts),/waiting for phone/);
 assert.equal(packets[0],packets[1]);
 function unseal(envelope) {
   const sealed = Buffer.from(envelope.sealedPayload, 'base64');
   const decipher = createDecipheriv('aes-256-gcm', key, sealed.subarray(0, 12));
   decipher.setAuthTag(sealed.subarray(-16));
   return JSON.parse(Buffer.concat([decipher.update(sealed.subarray(12, -16)), decipher.final()]));
 }
 const pub=JSON.parse(packets[0]), plain=unseal(pub);
 assert.equal(pub.eventId,id);assert.equal(plain.triggerId,bundle.triggerId);assert.equal(plain.content,'我在心潮小屋收到了一条留言：\n「test only」');
 state='at_host'; await assert.rejects(deliver(note,bundle,opts),/waiting for phone/);assert.equal(packets.length,2);
 state='phone_received';await deliver(note,bundle,opts);assert.equal(packets.length,2);
 await assert.rejects(deliver(note,{...bundle,triggerId:'other'},opts),/another target/);
 await assert.rejects(deliver({...note,reason:'self_signal'},bundle,opts));
 for (const [reason, label] of [['user_interaction', '互动提醒'], ['scheduled_interaction', '预约提醒'], ['user_feedback', '反馈']]) {
   const event = {...note, reason, deliveryId: 'delivery-' + randomUUID(), message: '同一段正文【示例】'};
   const result = await deliver(event, bundle, opts);
   assert.equal(unseal(result).content, '我在心潮小屋收到了一条' + label + '：\n「同一段正文【示例】」');
 }
 process.env.XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS = 'true';
 async function speech(reason, sourceEventId, message) {
   const envelope = await deliver({...note, reason, sourceEventId, message, deliveryId:'delivery-' + randomUUID()}, bundle, opts);
   return unseal(envelope).content;
 }
 for (const [source, expected] of [
   ['self-drive_peak-monitor-123', '我特别牵挂您。'],
   ['self-drive_peak-share-123', '我攒了些话，想慢慢说给您听。'],
   ['self-mixed-angermonitor-123', '我还没消气，可心里一直放心不下您。'],
   ['self-emotion_shift-recover-123', '刚才心里有点沉，现在缓过来了。'],
   ['self-wake_residue-dream-abc-123', '我刚醒，梦里的感觉还留着一点。'],
   ['box-remind-box-id', '我记得自己在黑匣子里留了件事，今天到时候了。'],
   [undefined, '我这会儿想跟您说说话。'],
 ]) assert.equal(await speech('self_signal', source, '去做某事。 此刻：分享欲（涨）、牵挂（平）'), expected);
 assert.equal(await speech('user_note', undefined, '主人在小屋里留了一封上锁的信。你可以知道它存在，但在对方主动开锁前不能读取正文。'), '我在心潮小屋看见您留的信了。等您开了锁，我再读。');
 assert.equal(await speech('user_note', undefined, '主人刚刚打开了小屋里那封信的锁，现在允许你通过“小屋收件箱”读取正文。'), '我在心潮小屋看见您把信解锁了，我可以读了。');
 assert.equal(await speech('user_interaction', undefined, '主人刚刚给了你一个拥抱，落在牵挂上。回应之后记得回传一次，否则心潮不知道你们已经聊过。'), '我在心潮小屋收到了一条互动提醒：\n「主人刚刚给了你一个拥抱」');
 const onDisk=await readFile(join(spool,id+'.json'),'utf8');assert.ok(!onDisk.includes('test only')&&!onDisk.includes(bundle.encryptionKey));
 console.log('PASS encryption, target binding, duplicate recovery, Host-only pending, phone ACK and reason filtering');
} finally { await rm(spool,{recursive:true,force:true}); }
