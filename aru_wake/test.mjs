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
 assert.equal(pub.eventId,id);assert.equal(plain.triggerId,bundle.triggerId);assert.equal(plain.content,'【小屋桥·留话｜心潮 MCP】test only（小屋桥递的，心里有数就行，不用特意回。）');
 state='at_host'; await assert.rejects(deliver(note,bundle,opts),/waiting for phone/);assert.equal(packets.length,2);
 state='phone_received';await deliver(note,bundle,opts);assert.equal(packets.length,2);
 await assert.rejects(deliver(note,{...bundle,triggerId:'other'},opts),/another target/);
 await assert.rejects(deliver({...note,reason:'self_signal'},bundle,opts));
 for (const [reason, label] of [['user_interaction', '互动'], ['scheduled_interaction', '预约'], ['user_feedback', '反馈']]) {
   const event = {...note, reason, deliveryId: 'delivery-' + randomUUID(), message: '同一段正文【示例】'};
   const result = await deliver(event, bundle, opts);
   assert.equal(unseal(result).content, '【小屋桥·' + label + '｜心潮 MCP】同一段正文【示例】（小屋桥递的，心里有数就行，不用特意回。）');
 }
 process.env.XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS = 'true';
 const self = await deliver({...note, reason: 'self_signal', deliveryId: 'delivery-' + randomUUID()}, bundle, opts);
 assert.match(unseal(self).content, /^【此刻，[^】]+的状态】\ntest only$/u);
 const onDisk=await readFile(join(spool,id+'.json'),'utf8');assert.ok(!onDisk.includes('test only')&&!onDisk.includes(bundle.encryptionKey));
 console.log('PASS encryption, target binding, duplicate recovery, Host-only pending, phone ACK and reason filtering');
} finally { await rm(spool,{recursive:true,force:true}); }
