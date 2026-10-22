"""모델 요청 투영 — 세션 원문은 보존하고 모델에 보내는 요청 사본만 줄인다.

ADK는 세션 이벤트를 deepcopy해 LlmRequest.contents를 만들므로(contents.py) 여기서 바꾼 것은
요청에만 실리고 세션·출처 카드·인용 검증·병렬 유실 복구는 원문을 본다. 무엇을 어떻게 줄일지는
toolset이 선언한다(toolsets.MODEL_INPUT_PROJECTORS, ResolvedApp.model_input_projectors) — 이
모듈은 도구명·출처 타입을 모른다. 투영은 결정적이라 같은 이력이면 요청 바이트가 같다(암묵
캐시 프리픽스 불변). 사각: ADK 이력 압축 요약 호출(llm_event_summarizer)은 콜백을 우회한다.
"""

from collections.abc import Callable, Mapping


def make_projection_callback(projectors: Mapping[str, Callable]):
    """before_model_callback — 각 function_response를 그 도구의 투영기로 바꾼 사본을 싣는다.

    투영기가 같은 객체(is)를 돌려주면 무변경이다. 마지막 Content는 세션 이벤트와 객체를 공유할 수
    있어 제자리 수정하지 않고 model_copy 사본으로 바꾼다. None을 돌려 모델 호출을 그대로 진행한다.
    """

    def project_model_input(callback_context, llm_request):
        contents = list(llm_request.contents)
        for content_index, content in enumerate(contents):
            parts = list(content.parts or [])
            changed = False
            for part_index, part in enumerate(parts):
                response = part.function_response
                projector = projectors.get(response.name) if response is not None else None
                if projector is None:
                    continue
                projected = projector(response.response)
                if projected is response.response:
                    continue
                parts[part_index] = part.model_copy(update={
                    "function_response": response.model_copy(update={"response": projected})
                })
                changed = True
            if changed:
                contents[content_index] = content.model_copy(update={"parts": parts})
        llm_request.contents = contents
        return None

    return project_model_input
