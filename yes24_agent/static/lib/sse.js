// SSE 스트림 읽기 — 채팅·매트릭스 공용 단일 구현(두 페이지에 같은 파서·리더 루프가 복제돼 있었다).

// SSE 이벤트 블록(event:/data:)을 {event, data}로 파싱한다. data가 JSON이 아니면 {}.
export function parseEvent(block) {
  let event = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  let parsed = {};
  if (data) {
    try { parsed = JSON.parse(data); } catch (e) { parsed = {}; }
  }
  return { event, data: parsed };
}

// 응답 본문을 끝까지 읽으며 이벤트마다 onEvent(event, data)를 부른다.
// 중단(AbortError)·네트워크 오류는 호출자에게 그대로 던진다(중지/실패 처리는 페이지의 몫).
export async function readEventStream(response, onEvent) {
  if (!response.ok || !response.body) throw new Error("서버 응답 오류: " + response.status);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (!block.trim()) continue;
      const { event, data } = parseEvent(block);
      onEvent(event, data);
    }
  }
}
