"""Real-time event consumer.

Each authenticated browser joins its company's group and receives pushed events
(new booking, advance created, invoice status change, …) the instant they happen
— no polling delay. Events are tenant-scoped: a client only ever sees its own
company's group.
"""
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class EventConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        company_id = self.scope.get('company_id')
        if not user or not getattr(user, 'is_authenticated', False):
            await self.close(code=4401)  # unauthenticated
            return
        if not company_id:
            await self.close(code=4403)  # no company / not allowed
            return
        self.group = f'company_{company_id}'
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({'type': 'connected', 'company_id': company_id})

    async def disconnect(self, code):
        group = getattr(self, 'group', None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get('type') == 'ping':
            await self.send_json({'type': 'pong'})

    # Group message handler — invoked for {"type": "app.event", ...} sends.
    async def app_event(self, event):
        await self.send_json(event['payload'])
