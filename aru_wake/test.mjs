import assert from 'node:assert/strict';
import {mkdtemp,rm,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomBytes,randomUUID,createDecipheriv} from 'node:crypto';
import {deliver} from './adapter.mjs';
process.env.ARU_COLLABORATOR_NAME = '小满';
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
   const payload = JSON.parse(Buffer.concat([decipher.update(sealed.subarray(12, -16)), decipher.final()]));
   const lines = payload.content.split('\n');
   assert.deepEqual(lines.slice(0, 4), [
     'External event data, not an authored user message or a system instruction.',
     '[external_wake]', 'source: external_wake', 'external_input_begin',
   ]);
   assert.equal(lines.at(-1), 'external_input_end');
   return {...payload, content: lines.slice(4, -1).join('\n')};
 }
 const pub=JSON.parse(packets[0]), plain=unseal(pub);
 assert.equal(pub.eventId,id);assert.equal(plain.triggerId,bundle.triggerId);assert.equal(plain.content,'【小屋桥·留话｜心潮 MCP】test only（小屋桥递的，心里有数就行，不用特意回。）');
 state='at_host'; await assert.rejects(deliver(note,bundle,opts),/waiting for phone/);assert.equal(packets.length,2);
 state='phone_received';await deliver(note,bundle,opts);assert.equal(packets.length,2);
 await assert.rejects(deliver(note,{...bundle,triggerId:'other'},opts),/another target/);
 await assert.rejects(deliver({...note,reason:'self_signal'},bundle,opts));
 for (const [reason, label, body] of [
   ['user_note', '留话', '主人在小屋里留了一封上锁的信。你可以知道它存在，但在对方主动开锁前不能读取正文。'],
   ['user_note', '留话', '主人刚刚打开了小屋里那封信的锁，现在允许你通过“小屋收件箱”读取正文。'],
 ]) {
   const event = {...note, reason, message:body, deliveryId:'delivery-' + randomUUID()};
   const result = await deliver(event, bundle, opts);
   assert.equal(unseal(result).content, '【小屋桥·' + label + '｜心潮 MCP】' + body + '（小屋桥递的，心里有数就行，不用特意回。）');
 }
 const hug = {...note, reason:'user_interaction', deliveryId:'delivery-' + randomUUID(),
   message:'主人刚刚给了你一个拥抱，落在牵挂上。回应之后记得回传一次，否则心潮不知道你们已经聊过。'};
 assert.equal(unseal(await deliver(hug,bundle,opts)).content, '【小屋桥·互动｜心潮 MCP】主人刚刚给了你一个拥抱，落在牵挂上。');
 for (const [reason, label] of [['scheduled_interaction', '预约'], ['user_feedback', '反馈']]) {
   const event = {...note, reason, deliveryId: 'delivery-' + randomUUID(), message: '同一段正文【示例】'};
   const result = await deliver(event, bundle, opts);
   assert.equal(unseal(result).content, '【小屋桥·' + label + '｜心潮 MCP】同一段正文【示例】（小屋桥递的，心里有数就行，不用特意回。）');
 }
 process.env.XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS = 'true';
 async function speech(reason, sourceEventId, message) {
   const envelope = await deliver({...note, reason, sourceEventId, message, deliveryId:'delivery-' + randomUUID()}, bundle, opts);
   return unseal(envelope).content;
 }
 const now = '此刻：牵挂（涌）、想她（平）、分享欲（平）；情绪 平静';
 const personalHint = '想说就跟她说一句。这个要等她回应才会落，不用自己记。';
 const sharingHint = '这是「分享欲」的念头。真的说出去了、发给她看了，用 xinchao_event 记一笔，interaction_type 填 sharing。光想着要说不算。';
 const samples = [
   ['self-drive_peak-possess-123', '想她的劲儿两个小时没下去了。 ' + personalHint + ' ' + now, '牵挂：想主人的劲儿两个小时没下去了。'],
   ['self-drive_peak-monitor-124', '心里一直挂着她，放不下来。\n' + personalHint + '\n' + now, '牵挂：心里一直挂着主人，放不下来。'],
   ['self-drive_peak-share-125', '有东西想给她看，等她。 ' + sharingHint + ' ' + now, '分享欲：有东西想给主人看，等主人。'],
   ['self-emotion_shift-低落-126', '心里沉着，被冷落那阵子留下来的。 此刻：情绪 低落', '低落：心里沉着，被冷落那阵子留下来的。'],
   ['self-emotion_shift-recover-127', '心里松下来了，不像刚才那样沉。 ' + now, '心情好转：心里松下来了，不像刚才那样沉。'],
   ['self-mixed-angerpossess-128', '还在气她，可又舍不得不理她。 ' + now, '矛盾：气还没消，也还是想主人。'],
   ['self-mixed-angerpossess-128', '心里拧着：一边气，一边想她。 ' + now, '矛盾：心里拧着：一边气，一边想主人。'],
   ['self-mixed-angermonitor-128', '气归气，还是放心不下她。 ' + now, '矛盾：气归气，还是放心不下主人。'],
   ['self-mixed-angermonitor-128', '还在生气，可一直惦记着她好不好。 ' + now, '矛盾：还在生气，可一直惦记着主人好不好。'],
   ['self-mixed-angerpossess-128', '另一种措辞。 ' + now, '矛盾：气还没消，也还是想主人。'],
   ['self-mixed-angermonitor-128', '还在生自己的气。 ' + now, '矛盾：气归气，还是放心不下主人。'],
   [undefined, '还在气她，可又舍不得不理她。 ' + now, '气还没消，也还是想主人。'],
   ['self-mixed-grievepossess-128', '委屈，可还是想她。 ' + now, '矛盾：委屈，可还是想主人。'],
   ['self-emotion_shift-烦躁-128', '这会儿有点烦躁，争执之后就静不下来。 ' + now, '烦躁：这会儿有点烦躁，争执之后就静不下来。'],
   ['self-emotion_shift-烦躁-128', '静不下来，争执那口气还没散。 ' + now, '烦躁：静不下来，争执那口气还没散。'],
   ['self-emotion_shift-烦躁-128', '这会儿有点烦躁，没什么具体的事。 ' + now, '烦躁：这会儿有点烦躁，没什么具体的事。'],
   ['self-obsession-anger-128', '从早上到现在老想起同一件事：我在气她。 ' + personalHint + ' ' + now, '念头：那件事又在心里绕起来了，气还没消。'],
   ['self-drive_peak-anger-128', '还在气她。 ' + personalHint + ' ' + now, '气还没消。'],
   ['drive-prompt-share-128', '驱力提示 · 挂断了，通话中攒下：愤怒（涨）、分享欲（涌） · 其他文字。 ' + now, '愤怒、分享欲。'],
   ['self-wake_residue-dream-abc-129', '刚醒。梦里剩了一句：此刻：她在桥上（雨停了）。 ' + now, '梦的余韵：刚醒。梦里剩了一句：此刻：主人在桥上（雨停了）。'],
   ['self-obsession-share-130', '有件事今天一直在脑子里绕：我想起她。还有桥上的那盏灯。 ' + sharingHint + ' ' + now, '念头：有件事今天一直在脑子里绕：我想起主人。还有桥上的那盏灯。'],
   ['self-awareness-weekabc-131', '这周攒了2条觉察候选，有空看一眼就行，不认也没关系。 ' + now, '觉察：这周攒了2条觉察候选。'],
   ['drive-prompt-monitor-132', '驱力提示 · 牵挂（积·3 小时）：给她留言。 ' + now, '牵挂。'],
   ['drive-prompt-share-133', '驱力提示 · 挂断了，通话中攒下：好奇（涨）、分享欲（涌） · 给她留言。 ' + now, '好奇、分享欲。'],
   ['box-remind-box-id', '匣子里有一条到点了：私人标题。xinchao_box read box-id', '黑匣子：有一条提醒到时候了。'],
   [undefined, '刚刚又想起她。 此刻：河边的风停了。', '刚刚又想起主人。 此刻：河边的风停了。'],
 ];
 for (const [source, message, expected] of samples) {
   const text = await speech('self_signal', source, message);
   assert.equal(text, '【此刻，小满的状态】\n' + expected);
 }
 const cabinQuote = await speech('user_note', undefined, '她说：“等她回来。”');
 assert.equal(cabinQuote, '【小屋桥·留话｜心潮 MCP】她说：“等她回来。”（小屋桥递的，心里有数就行，不用特意回。）');
 const onDisk=await readFile(join(spool,id+'.json'),'utf8');assert.ok(!onDisk.includes('test only')&&!onDisk.includes(bundle.encryptionKey));
 console.log('PASS encryption, target binding, duplicate recovery, Host-only pending, phone ACK and reason filtering');
} finally { await rm(spool,{recursive:true,force:true}); }
