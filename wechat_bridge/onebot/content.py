"""Validate the entire OneBot chain before persistent reservation or I/O."""
from .codec import number, UnsupportedMessageSegment
from ..core.text import split_text, text
from ..core.sending import PreparedSend
from .. import image_output as output_images
from .. import voice_output as output_voice


class ContentCodec:
    def __init__(self, messages, *, segment_bytes=8192, image_output=False, voice_output=False):
        self.messages = messages
        self.segment_bytes = segment_bytes
        self.image_output, self.voice_output = image_output, voice_output

    @property
    def state(self):
        return self.messages.state

    @property
    def backend(self):
        return self.messages.backend

    @property
    def peer(self):
        return self.messages.peer

    def text(self, params, destination=None):
        value = params.get('message')
        if isinstance(value, str):
            if '[CQ:' in value and params.get('auto_escape') is not True:
                raise UnsupportedMessageSegment('unsupported_segment')
            result = value
        elif isinstance(value, list) and 0 < len(value) <= 128:
            parts = []
            for segment in value:
                if not isinstance(segment, dict) or set(segment) != {'type', 'data'} or not isinstance(segment['data'], dict):
                    raise ValueError('invalid_segment')
                data = segment['data']
                if segment['type'] == 'text' and set(data) == {'text'} and isinstance(data['text'], str):
                    parts.append(data['text'])
                elif (segment['type'] == 'reply' and set(data) == {'id'}
                      and self.state.message_route(number(data['id'])) == (destination or self.peer)):
                    pass
                else:
                    raise UnsupportedMessageSegment('unsupported_segment')
            result = ''.join(parts)
        else:
            raise ValueError('invalid_message')
        return result, split_text(result, self.segment_bytes)

    def media(self, destination, params, media):
        contract = output_images if media == 'image' else output_voice
        enabled = self.image_output if media == 'image' else self.voice_output
        segment_type = 'image' if media == 'image' else 'record'
        segments = params.get('message')
        if not enabled or not isinstance(segments, list) or not 1 <= len(segments) <= 2:
            raise UnsupportedMessageSegment('image_output_disabled')
        image, quote = None, None
        try:
            for segment in segments:
                if (not isinstance(segment, dict) or set(segment) != {'type', 'data'}
                        or not isinstance(segment['data'], dict)):
                    raise contract.OutputError('output_' + media + '_segment')
                if segment['type'] == segment_type and image is None:
                    image = contract.from_segment(segment)
                elif (segment['type'] == 'reply' and quote is None and set(segment['data']) == {'id'}
                      and self.state.message_route(number(segment['data']['id'])) == destination):
                    quote = str(number(segment['data']['id']))
                else:
                    raise contract.OutputError('output_' + media + '_segment')
            if image is None:
                raise contract.OutputError('output_' + media + '_segment')
        except (ValueError, TypeError) as error:
            reason = {'output_image_source': 'source', 'output_image_size': 'size',
                'output_image_base64': 'base64', 'output_image_format': 'format',
                'output_image_limits': 'dimensions', 'output_image_interlace': 'interlace',
                'output_image_segment': 'segment'}.get(str(error), 'payload')
            if media == 'image':
                self.backend.report('image_output_validation', detail=output_images.rejection(reason, segments))
            else:
                self.backend.report('voice_output_validation', code=str(error) if str(error) in output_voice.ERRORS else 'output_voice_payload')
            raise UnsupportedMessageSegment('output_image_invalid') from None
        return contract, image, quote

    @staticmethod
    def media_timeout(media, payload):
        return output_images.transfer_window(payload) + 8 if media == 'image' else 180

    def prepare(self, destination, params):
        segments = params.get('message')
        if isinstance(segments, list):
            for segment_type, media in (('image', 'image'), ('record', 'voice')):
                if any(isinstance(segment, dict) and segment.get('type') == segment_type for segment in segments):
                    proof, payload, quote = self.media(destination, params, media)
                    return PreparedSend(media, payload=payload, proof=proof, quote=quote,
                                        timeout=self.media_timeout(media, payload))
        body, parts = self.text(params, destination)
        quote = None
        if isinstance(segments, list):
            quote = next((str(number(segment['data']['id'])) for segment in segments
                          if segment['type'] == 'reply'), None)
        return PreparedSend('text', body=body, parts=tuple(parts), quote=quote)


def legacy_text(self, params):
    value = params.get('message')
    if isinstance(value, str):
        if '[CQ:' in value and params.get('auto_escape') is not True:
            raise UnsupportedMessageSegment('unsupported_segment')
        output = value
    elif isinstance(value, list) and 0 < len(value) <= 32:
        parts = []
        for segment in value:
            if not isinstance(segment, dict) or set(segment) != {'type', 'data'} or not isinstance(segment['data'], dict):
                raise ValueError('invalid_message')
            data = segment['data']
            if segment['type'] == 'text' and set(data) == {'text'} and isinstance(data['text'], str):
                parts.append(data['text'])
            elif segment['type'] == 'reply' and set(data) == {'id'} and self.pending is not None:
                if number(data['id']) != self.pending['event']['message_id']:
                    raise ValueError('reply_mismatch')
            else:
                raise UnsupportedMessageSegment('unsupported_segment')
        output = ''.join(parts)
    else:
        raise ValueError('invalid_message')
    if not text(output) or not output.strip():
        raise ValueError('invalid_message')
    return output
