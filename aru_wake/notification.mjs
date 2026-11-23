import { renderText } from './runtime/examples/render.mjs';

const driveLabels = new Map(Object.entries({
  possess: '牵挂', monitor: '牵挂', share: '分享欲', libido: '亲近',
  curiosity: '好奇', boredom: '无聊', duty: '野心', reflection: '反思',
  anger: '生气', grieve: '难过', favored: '想被偏爱',
}));
const signalLabels = new Map(Object.entries({
  longing: '牵挂', wake_residue: '梦的余韵', mixed: '矛盾',
  awareness: '觉察', obsession: '念头',
}));

// Only the two drives are known when a mixed signal fires. Keep the neutral
// native lines and use the drives alone if a different template assigns blame.
const mixedAngerText = new Set([
  '心里拧着：一边气，一边想她。',
  '气归气，还是放心不下她。',
  '还在生气，可一直惦记着她好不好。',
]);
const mixedAngerFallback = new Map([
  ['angerpossess', '气还没消，也还是想她。'],
  ['angermonitor', '气归气，还是放心不下她。'],
]);

function stateNotice({message, sourceEventId = ''}) {
  const drive = /^(?:self-drive_peak|drive-prompt)-([a-z_]+)-/.exec(sourceEventId);
  const signal = /^self-([a-z_]+)-(.+)-\d+$/.exec(sourceEventId);
  const kind = signal?.[1];
  let label = driveLabels.get(drive?.[1]) ?? signalLabels.get(kind);
  if (kind === 'emotion_shift') label = signal[2] === 'recover' ? '心情好转' : signal[2];

  // The native queue folds the separate description, response hint, and now-line
  // into a single space-delimited string. Only remove generated suffixes.
  let detail = message.trim();
  const now = detail.lastIndexOf('此刻：');
  if (now >= 0 && (now === 0 || /\s/u.test(detail[now - 1])) &&
      /^(?:情绪\s|[^\s（，；、]{1,16}（[静涨涌平落积](?:·[^）]+)?）)/u.test(detail.slice(now + 3))) {
    detail = detail.slice(0, now).trimEnd();
  }
  detail = detail.replace(/\s+(?:想说就跟她说一句。这个要等她回应才会落，不用自己记。|这是「[^」]+」的念头。真的[^\n]*?用 xinchao_event 记一笔，interaction_type 填 [a-z_]+。[^\n]*)$/u, '');

  if (kind === 'mixed' && signal[2].startsWith('anger') && !mixedAngerText.has(detail)) {
    detail = mixedAngerFallback.get(signal[2]) ?? '心里还有气。';
  } else if (!sourceEventId && detail === '还在气她，可又舍不得不理她。') {
    detail = mixedAngerFallback.get('angerpossess');
  }
  if (kind === 'obsession' && signal[2] === 'anger') {
    detail = '那件事又在心里绕起来了，气还没消。';
  }
  if (kind === 'drive_peak' && drive?.[1] === 'anger') return '气还没消。';
  if (kind === 'awareness') {
    detail = detail.replace(/，有空看一眼就行，不认也没关系。$|，我自己看看是不是；不是就放下。$/u, '。');
  }
  if (sourceEventId.startsWith('box-remind-')) {
    label = '黑匣子';
    detail = '有一条提醒到时候了。';
  }
  if (sourceEventId.startsWith('drive-prompt-')) {
    // Action prompts can contain a configured script. Report only the drives
    // actually named in this prompt (including a batch released after a call).
    const active = [...detail.matchAll(/([^\s·：，；、（）]+)（[涌涨积](?:·[^）]+)?）/gu)].map(m => m[1]);
    detail = [...new Set(active)].join('、') || label || '有一股心绪浮了上来';
    return detail + '。';
  }
  return detail && label ? label + '：' + detail : detail || label || '心潮有了变化。';
}

export function aruNotificationText(delivery) {
  if (delivery.reason === 'user_interaction') {
    const message = delivery.message.trim().replace(/\s+/gu, ' ')
      .replace(/回应之后记得回传一次，否则心潮不知道你们已经聊过。$/u, '');
    return '【小屋桥·互动｜心潮 MCP】' + message;
  }
  if (delivery.reason !== 'self_signal') return renderText(delivery).replace('】', '｜心潮 MCP】');
  const name = process.env.ARU_COLLABORATOR_NAME?.trim() || '小禾';
  const state = stateNotice(delivery).replaceAll('她', '主人');
  return `【此刻，${name}的状态】\n${state}`;
}
