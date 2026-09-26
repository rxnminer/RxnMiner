export function modelLogo(lane: string): string | null {
  lane = lane.toLowerCase();
  if (lane.includes('deepseek')) return 'deepseek';
  if (lane.includes('glm')) return 'glm';
  if (lane.includes('kimi')) return 'kimi';
  if (lane.includes('doubao')) return 'doubao';
  if (lane.includes('opus') || lane.includes('claude')) return 'claude';
  if (lane.includes('qwen')) return 'qwen';
  if (lane.includes('gpt')) return 'openai';
  if (lane.includes('gemini')) return 'gemini';
  return null;
}
