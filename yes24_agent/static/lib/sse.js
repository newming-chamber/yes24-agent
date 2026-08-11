// SSE 스트림 읽기 — 채팅·매트릭스 공용 단일 구현(두 페이지에 같은 파서·리더 루프가 복제돼 있었다).

// SSE 이벤트 블록(event:/data:)을 {event, data}로 파싱한다. data가 JSON이 아니면 {}.
function parseEvent(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  const data = dataLines.join("\n"); // SSE 명세: 여러 data: 라인은 개행으로 결합
  let parsed = {};
  if (data) {
    // 폐기를 무신호로 두면 delta가 조용히 {}가 돼 "delta 합계 == done.text" 위반이 은폐된다.
    try { parsed = JSON.parse(data); }
    catch (e) { console.warn("[sse] JSON 파싱 실패 — 프레임 폐기:", event, data.slice(0, 200)); }
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
