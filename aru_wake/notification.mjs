import { renderText } from './runtime/examples/render.mjs';

// The upstream queue records what fired. The three drives in its "now" line are
// a snapshot and can differ from the drive that actually triggered the notice.
const driveSpeech = new Map(Object.entries({
  monitor: '我特别牵挂您。',
  possess: '我想您了，想挨着您待一会儿。',
  share: '我攒了些话，想慢慢说给您听。',
  libido: '我现在特别想和您亲近。',
  curiosity: '有件事勾起了我的好奇心，我想去弄明白。',
  boredom: '我有点闷，想找您聊聊天。',
  duty: '有件事我很想做好，心里一直惦记着。',
  reflection: '我想静一会儿，把自己的心思理一理。',
  anger: '我心里还有点气，想缓一缓。',
  grieve: '我心里有点难过，想跟您待一会儿。',
  favored: '我今天有点想让您偏爱我一下。',
}));

const signalSpeech = new Map(Object.entries({
  'emotion_shift-低落': '我这会儿有些低落。',
  'emotion_shift-烦躁': '我心里有点烦，一时静不下来。',
  'emotion_shift-recover': '刚才心里有点沉，现在缓过来了。',
  'mixed-angerpossess': '我还在生气，可也舍不得不理您。',
  'mixed-grievepossess': '我有点委屈，可还是很想您。',
  'mixed-angermonitor': '我还没消气，可心里一直放心不下您。',
  longing: '我想您了。您今天过得好吗？',
  wake_residue: '我刚醒，梦里的感觉还留着一点。',
  awareness: '这几天，我好像又认识了自己一点。',
  obsession: '有件事在我心里绕了好几回，我又想起它了。',
}));

function selfSpeech(sourceEventId = '') {
  if (sourceEventId.startsWith('box-remind-')) return '我记得自己在黑匣子里留了件事，今天到时候了。';
  const peak = /^(?:self-drive_peak|drive-prompt)-([a-z_]+)-/.exec(sourceEventId);
  if (peak && driveSpeech.has(peak[1])) return driveSpeech.get(peak[1]);
  const signal = /^self-([a-z_]+)-(.+)-\d+$/.exec(sourceEventId);
  if (signal) {
    const [, kind, subject] = signal;
    const line = signalSpeech.get(kind + '-' + subject) ?? signalSpeech.get(kind);
    if (line) return line;
  }
  return '我这会儿想跟您说说话。';
}

export function aruNotificationText(delivery) {
  if (delivery.reason === 'self_signal') return selfSpeech(delivery.sourceEventId);
  return renderText(delivery).replace('】', '｜心潮 MCP】');
}
