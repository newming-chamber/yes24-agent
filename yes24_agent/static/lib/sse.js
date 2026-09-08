// SSE 스트림 읽기 — 채팅·매트릭스 공용 단일 구현(두 페이지에 같은 파서·리더 루프가 복제돼 있었다).

// 잘못된 JSON은 정상 빈 이벤트로 위장하지 않고 호출자의 실패 경로로 보낸다.
function parseEvent(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  const data = dataLines.join("\n"); // SSE 명세: 여러 data: 라인은 개행으로 결합
  if (!dataLines.length) return null;
  return { event, data: data ? JSON.parse(data) : {} };
}

// onEvent가 false를 반환하면 해당 프로토콜의 종료 이벤트를 받은 것이므로 읽기를 끝낸다.
// 중단(AbortError)·네트워크 오류는 호출자에게 그대로 던진다(중지/실패 처리는 페이지의 몫).
export async function readEventStream(response, onEvent) {
  if (!response.ok || !response.body) throw new Error("서버 응답 오류: " + response.status);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = () => {
    let boundary;
    while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
      const block = buffer.slice(0, boundary.index).replace(/\r\n/g, "\n");
      buffer = buffer.slice(boundary.index + boundary[0].length);
      const frame = parseEvent(block);
      if (frame && onEvent(frame.event, frame.data) === false) return false;
    }
    return true;
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (!dispatch()) return;
    }
    buffer += decoder.decode();
    if (!dispatch()) return;
    if (buffer.split(/\r?\n/).some((line) => line.trim() && !line.startsWith(":"))) {
      throw new Error("완료되지 않은 SSE 프레임");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
