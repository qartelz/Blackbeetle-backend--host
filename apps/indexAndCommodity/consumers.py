from channels.generic.websocket import AsyncWebsocketConsumer
from asgiref.sync import sync_to_async
from django.core.cache import cache
from typing import Dict, List, Optional, Set
import json
from decimal import Decimal
import logging
import asyncio
from urllib.parse import parse_qs
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.db import transaction
from apps.subscriptions.models import Subscription
from .models import Trade, IndexAndCommodity
from django.db import models
import traceback

logger = logging.getLogger(__name__)

# Database-specific sync_to_async decorator
db_sync_to_async = sync_to_async(thread_sensitive=True)

class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle Decimal objects."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)

class IndexAndCommodityUpdateManager:
    """Utility class for managing trade caching and plan level access."""
    
    CACHE_TIMEOUT = 300  # Cache duration in seconds (5 minutes)

    @staticmethod
    async def get_cached_trades(cache_key: str) -> Optional[Dict]:
        """Retrieve cached trade data asynchronously."""
        try:
            return await sync_to_async(cache.get)(cache_key)
        except Exception as e:
            logger.error(f"Failed to get cached trades: {str(e)}")
            return None

    @staticmethod
    async def set_cached_trades(cache_key: str, data: Dict):
        """Store trade data in the cache asynchronously."""
        try:
            await sync_to_async(cache.set)(cache_key, data, IndexAndCommodityUpdateManager.CACHE_TIMEOUT)
        except Exception as e:
            logger.error(f"Failed to set cached trades: {str(e)}")

    @staticmethod
    def get_plan_levels(plan_type: str) -> List[str]:
        """Get accessible plan levels for a given plan type."""
        return {
            'BASIC': ['BASIC'],
            'PREMIUM': ['BASIC', 'PREMIUM'],
            'SUPER_PREMIUM': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM'],
            'FREE_TRIAL': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM']
        }.get(plan_type, [])

class IndexAndCommodityUpdatesConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for delivering real-time index and commodity trade updates."""
    
    RECONNECT_DELAY = 2
    MAX_RETRIES = 3
    MESSAGE_DEDUPLICATION_TIMEOUT = 5

    ERROR_MESSAGES = {
        4001: "No authentication token provided. Please log in and try again.",
        4002: "Invalid or expired token. Please log in again.",
        4003: "Authentication failed. Please verify your credentials.",
        4004: "An unexpected error occurred during authentication.",
        4005: "No active subscription found. Please subscribe to continue.",
        4006: "Failed to set up trade updates. Please try again later.",
        4007: "Maximum connection retries exceeded. Please check your network and try again."
    }

    SUCCESS_MESSAGES = {
        "connected": "Successfully connected to index and commodity updates.",
        "initial_data": "Initial trade data loaded successfully.",
        "trade_update": "Trade update received successfully.",
        "refresh_complete": "Data refresh completed successfully."
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.subscription = None
        self.trade_manager = IndexAndCommodityUpdateManager()
        self.is_connected = False
        self.connection_retries = 0
        self.user_group = None
        self._initial_data_task = None
        self.processed_messages = set()
        self.trade_limits = {
            'BASIC': {
                'new': 6,
                'previous': 6,
                'total': 12
            },
            'PREMIUM': {
                'new': 9,
                'previous': 6,
                'total': 15
            },
            'SUPER_PREMIUM': {
                'new': None,
                'previous': None,
                'total': None
            },
            'FREE_TRIAL': {
                'new': None,
                'previous': None,
                'total': None
            }
        }
        self.cache = cache
        self.cache_timeout = 300

    async def connect(self):
        """Handle WebSocket connection establishment."""
        if self.connection_retries >= self.MAX_RETRIES:
            await self.close(code=4007)
            return

        try:
            await self.accept()
            
            if not await self._authenticate():
                await self.close(code=4003)
                return

            await self._clear_user_cache()
            
            self.is_connected = True
            await self.send_success("connected")
            
            if await self._setup_user_group():
                self._initial_data_task = asyncio.create_task(self.send_initial_data())

        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            self.connection_retries += 1
            await asyncio.sleep(self.RECONNECT_DELAY)
            await self.connect()

    async def send_error(self, code: int, extra_info: str = None):
        """Send an error message to the client."""
        message = self.ERROR_MESSAGES.get(code, "An unexpected error occurred.")
        if extra_info:
            message += f" Details: {extra_info}"
        await self.send(text_data=json.dumps({
            "type": "error",
            "code": code,
            "message": message
        }))

    async def send_success(self, event: str, extra_info: str = None):
        """Send a success message to the client."""
        message = self.SUCCESS_MESSAGES.get(event, "Operation completed successfully.")
        if extra_info:
            message += f" {extra_info}"
        await self.send(text_data=json.dumps({
            "type": "success",
            "event": event,
            "message": message
        }))

    async def _authenticate(self) -> bool:
        """Authenticate the user using a JWT token."""
        try:
            token = self.scope['url_route']['kwargs'].get('token')
            
            if not token:
                query_string = self.scope.get('query_string', b'').decode('utf-8')
                parsed_qs = parse_qs(query_string)
                token = parsed_qs.get('token', [None])[0] or parsed_qs.get('access_token', [None])[0]

            if not token:
                await self.send_error(4001)
                return False

            jwt_auth = JWTAuthentication()
            validated_token = await sync_to_async(jwt_auth.get_validated_token)(token)
            self.user = await sync_to_async(jwt_auth.get_user)(validated_token)

            if not self.user or not self.user.is_authenticated:
                await self.send_error(4003)
                return False

            # Get active subscription
            self.subscription = await self._get_active_subscription(self.user)
            if not self.subscription:
                await self.send_error(4005)
                return False

            return True

        except (InvalidToken, TokenError) as e:
            await self.send_error(4002, str(e))
            return False
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            await self.send_error(4004, str(e))
            return False

    @db_sync_to_async
    def _get_active_subscription(self, user):
        """Get user's active subscription."""
        today = timezone.now().date()
        return Subscription.objects.filter(
            user=user,
            is_active=True,
            end_date__gte=today
        ).first()

    async def _setup_user_group(self) -> bool:
        """Set up user's group for receiving updates."""
        try:
            self.user_group = f"trade_updates_{self.user.id}"
            await self.channel_layer.group_add(self.user_group, self.channel_name)
            return True
        except Exception as e:
            logger.error(f"Group setup error: {str(e)}")
            await self.send_error(4006)
            return False

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        if self.user_group:
            await self.channel_layer.group_discard(self.user_group, self.channel_name)
        if self._initial_data_task:
            self._initial_data_task.cancel()

    @db_sync_to_async
    def _get_trade_counts(self, bypass_cache=True):
        """Get current trade counts for the user."""
        cache_key = f"index_commodity_counts_{self.user.id}_{self.subscription.id}"
        cached_counts = None if bypass_cache else cache.get(cache_key)
        if cached_counts and not bypass_cache:
            return cached_counts

        try:
            plan_name = self.subscription.plan.name
            plan_levels = self.trade_manager.get_plan_levels(plan_name)
            subscription_start = self.subscription.start_date

            # Get NEW trades - created after subscription start
            new_trades = Trade.objects.filter(
                plan_type__in=plan_levels,
                status__in=['ACTIVE', 'COMPLETED', 'PENDING'],
                created_at__gte=subscription_start
            ).order_by('-created_at')[:6]  # Always limit to 6 for BASIC
            
            new_count = len(list(new_trades))
            
            # For PREVIOUS trades - created before subscription
            previous_trades = Trade.objects.filter(
                plan_type__in=plan_levels,
                status__in=['ACTIVE', 'COMPLETED', 'PENDING'],
                created_at__lt=subscription_start
            ).order_by('-created_at')[:6]  # Always limit to 6 for previous
            
            previous_count = len(list(previous_trades))
            
            # Calculate total (capped at total limit)
            total_count = min(new_count + previous_count, 12)  # Total limit for BASIC
            
            result = {
                'new': new_count,
                'previous': previous_count,
                'total': total_count
            }

            # Cache for 5 minutes
            cache.set(cache_key, result, 300)
            return result

        except Exception as e:
            logger.error(f"Error getting trade counts: {str(e)}")
            return {'new': 0, 'previous': 0, 'total': 0}

    async def _can_add_trade(self, is_new_trade=True):
        """Check if a new trade can be added based on current limits."""
        try:
            counts = await self._get_trade_counts(bypass_cache=True)
            plan_type = self.subscription.plan.name
            plan_limits = self.trade_limits.get(plan_type, {})

            if plan_type in ['SUPER_PREMIUM', 'FREE_TRIAL']:
                return True

            # For BASIC and PREMIUM users
            if is_new_trade:
                # Check new trade limit (6 for BASIC, 9 for PREMIUM)
                if counts['new'] >= plan_limits['new']:
                    logger.warning(f"New trade limit reached for {plan_type} user. Current count: {counts['new']}, Limit: {plan_limits['new']}")
                    return False
            else:
                # Check previous trade limit (6 for both BASIC and PREMIUM)
                if counts['previous'] >= plan_limits['previous']:
                    logger.warning(f"Previous trade limit reached for {plan_type} user. Current count: {counts['previous']}, Limit: {plan_limits['previous']}")
                    return False

            # Check total limit (12 for BASIC, 15 for PREMIUM)
            if counts['total'] >= plan_limits['total']:
                logger.warning(f"Total trade limit reached for {plan_type} user. Current count: {counts['total']}, Limit: {plan_limits['total']}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error checking trade limits: {str(e)}")
            return False

    @db_sync_to_async
    def _get_filtered_trade_data_sync(self, bypass_cache=False):
        """Synchronous part of getting filtered trade data."""
        try:
            # Get user's plan type and accessible plan levels
            plan_type = self.subscription.plan.name
            plan_limits = self.trade_limits.get(plan_type, {})
            plan_levels = self.trade_manager.get_plan_levels(plan_type)
            
            # Get subscription start datetime for precise filtering
            subscription_start = self.subscription.start_date

            # Get trades based on plan levels - only ACTIVE and COMPLETED trades
            all_trades = Trade.objects.select_related('index_and_commodity').filter(
                plan_type__in=plan_levels,  # This ensures we only get trades the user has access to
                status__in=['ACTIVE', 'COMPLETED']  # Only get ACTIVE and COMPLETED trades
            )

            # Split trades into new and previous based on subscription datetime
            new_trades_query = all_trades.filter(
                created_at__gte=subscription_start
            ).order_by('-created_at')
            
            previous_trades_query = all_trades.filter(
                created_at__lt=subscription_start
            ).order_by('-created_at')

            # Get total counts before limiting
            total_new_trades = new_trades_query.count()
            total_previous_trades = previous_trades_query.count()

            # Apply plan-specific limits
            if plan_type == 'BASIC':
                # Get exactly 6 previous trades
                previous_trades = list(previous_trades_query[:6])
                
                # Get exactly 6 new trades
                new_trades = list(new_trades_query[:6])
                
            elif plan_type == 'PREMIUM':
                # Get exactly 6 previous trades
                previous_trades = list(previous_trades_query[:6])
                
                # Get up to 9 new trades
                new_trades = list(new_trades_query[:9])
                
            else:  # SUPER_PREMIUM or FREE_TRIAL
                # No limits for these plans
                previous_trades = list(previous_trades_query)
                new_trades = list(new_trades_query)

            # Format trades
            formatted_new_trades = [self._format_trade(trade) for trade in new_trades]
            formatted_previous_trades = [self._format_trade(trade) for trade in previous_trades]

            # Calculate actual counts for shown trades
            shown_new = len(formatted_new_trades)
            shown_previous = len(formatted_previous_trades)
            shown_total = shown_new + shown_previous

            # Calculate remaining trades
            remaining_new = max(0, plan_limits.get('new', 0) - shown_new) if plan_limits.get('new') is not None else None
            remaining_previous = max(0, plan_limits.get('previous', 0) - shown_previous) if plan_limits.get('previous') is not None else None
            remaining_total = max(0, plan_limits.get('total', 0) - shown_total) if plan_limits.get('total') is not None else None

            return {
                'total_new_trades': total_new_trades,
                'total_previous_trades': total_previous_trades,
                'formatted_new_trades': formatted_new_trades,
                'formatted_previous_trades': formatted_previous_trades,
                'shown': {
                    'new': shown_new,
                    'previous': shown_previous,
                    'total': shown_total
                },
                'remaining': {
                    'new': remaining_new,
                    'previous': remaining_previous,
                    'total': remaining_total
                },
                'plan_type': plan_type,
                'plan_limits': plan_limits
            }

        except Exception as e:
            logger.error(f"Error getting filtered trade data: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    async def _get_filtered_trade_data(self, bypass_cache=False):
        """Get filtered trade data based on user's subscription."""
        cache_key = f"index_commodity_trades_{self.user.id}"
        
        if not bypass_cache:
            cached_data = await sync_to_async(cache.get)(cache_key)
            if cached_data:
                return cached_data

        # Get sync data
        sync_data = await self._get_filtered_trade_data_sync(bypass_cache)
        if not sync_data:
            return {
                "type": "initial_data",
                "counts": {
                    "total_available": {"new": 0, "previous": 0, "total": 0},
                    "shown": {"new": 0, "previous": 0, "total": 0},
                    "remaining": {"new": None, "previous": None, "total": None},
                    "limits": {"new": None, "previous": None, "total": None}
                },
                "stock_data": []
            }

        # Get trade counts
        trade_counts = await self._get_trade_counts()

        # Calculate remaining trades
        remaining = {
            'new': None if sync_data['plan_limits'].get('new') is None else max(0, sync_data['plan_limits']['new'] - trade_counts['new']),
            'previous': None if sync_data['plan_limits'].get('previous') is None else max(0, sync_data['plan_limits']['previous'] - trade_counts['previous']),
            'total': None if sync_data['plan_limits'].get('total') is None else max(0, sync_data['plan_limits']['total'] - trade_counts['total'])
        }

        # Prepare response with counts
        response_data = {
            "type": "initial_data",
            "counts": {
                "total_available": {
                    "new": sync_data['total_new_trades'],
                    "previous": sync_data['total_previous_trades'],
                    "total": sync_data['total_new_trades'] + sync_data['total_previous_trades']
                },
                "shown": trade_counts,
                "remaining": remaining,
                "limits": {
                    "new": sync_data['plan_limits'].get('new'),
                    "previous": sync_data['plan_limits'].get('previous'),
                    "total": sync_data['plan_limits'].get('total')
                }
            },
            "stock_data": sync_data['formatted_new_trades'] + sync_data['formatted_previous_trades']
        }

        await sync_to_async(cache.set)(cache_key, response_data, self.cache_timeout)
        return response_data

    def _format_trade(self, trade):
        """Format trade data for sending to client."""
        return {
            "id": trade.id,
            "tradingSymbol": trade.index_and_commodity.tradingSymbol,
            "exchange": trade.index_and_commodity.exchange,
            "instrumentName": trade.index_and_commodity.instrumentName,
            "intraday_trade": self._format_trade_details(trade) if trade.trade_type == 'INTRADAY' else None,
            "positional_trade": self._format_trade_details(trade) if trade.trade_type == 'POSITIONAL' else None,
            "created_at": trade.created_at.isoformat()
        }

    def _format_trade_details(self, trade):
        """Format trade details."""
        return {
            "id": trade.id,
            "trade_type": trade.trade_type,
            "status": trade.status,
            "plan_type": trade.plan_type,
            "warzone": str(trade.warzone),
            "image": trade.image.url if trade.image else None,
            "warzone_history": [
                {
                    "value": str(history.value),
                    "changed_at": history.changed_at.isoformat()
                }
                for history in trade.warzone_history_set.all()
            ] if hasattr(trade, 'warzone_history_set') else [],
            "analysis": self._format_analysis(trade.analysis.first()) if hasattr(trade, 'analysis') and trade.analysis.exists() else None,
            "trade_history": [
                {
                    "buy": str(history.buy),
                    "target": str(history.target),
                    "sl": str(history.sl),
                    "created_at": history.created_at.isoformat()
                }
                for history in trade.trade_history_set.all()
            ] if hasattr(trade, 'trade_history_set') else []
        }

    def _format_analysis(self, analysis):
        """Format analysis data."""
        if not analysis:
            return None
        return {
            "bull_scenario": analysis.bull_scenario,
            "bear_scenario": analysis.bear_scenario,
            "status": analysis.status,
            "completed_at": analysis.completed_at.isoformat() if analysis.completed_at else None,
            "created_at": analysis.created_at.isoformat(),
            "updated_at": analysis.updated_at.isoformat()
        }

    async def send_initial_data(self):
        """Send initial trade data to client."""
        try:
            response_data = await self._get_filtered_trade_data()
            await self.send(text_data=json.dumps(response_data, cls=DecimalEncoder))
            await self.send_success("initial_data")
        except Exception as e:
            logger.error(f"Error sending initial data: {str(e)}")
            await self.send_error(4006)

    async def trade_update(self, event):
        """Handle trade update events."""
        try:
            if not self._is_duplicate_message(event['data']):
                trade_data = event['data']
                
                # Check trade status - only process ACTIVE and COMPLETED trades
                trade_status = trade_data.get('trade_status')
                if trade_status not in ['ACTIVE', 'COMPLETED']:
                    logger.info(f"Ignoring trade update with status {trade_status}")
                    return
                
                # Get user's plan type and accessible plan levels
                user_plan_type = self.subscription.plan.name
                accessible_plan_levels = self.trade_manager.get_plan_levels(user_plan_type)
                
                # Check if user has access to this trade's plan type
                trade_plan_type = trade_data.get('plan_type')
                if trade_plan_type not in accessible_plan_levels:
                    logger.warning(f"User with plan {user_plan_type} cannot access trade of plan type {trade_plan_type}")
                    return
                
                # Check if this is a new trade
                trade_timestamp = timezone.datetime.fromisoformat(trade_data['timestamp'])
                is_new_trade = trade_timestamp >= self.subscription.start_date
                
                # Check if we can add this trade
                if not await self._can_add_trade(is_new_trade):
                    logger.warning(f"Trade limit reached. Ignoring trade update for trade_id: {trade_data.get('trade_id')}")
                    return

                # Get current trade counts
                trade_counts = await self._get_trade_counts(bypass_cache=True)
                plan_limits = self.trade_limits.get(user_plan_type, {})

                # Calculate remaining trades
                remaining = {
                    'new': None if plan_limits.get('new') is None else max(0, plan_limits['new'] - trade_counts['new']),
                    'previous': None if plan_limits.get('previous') is None else max(0, plan_limits['previous'] - trade_counts['previous']),
                    'total': None if plan_limits.get('total') is None else max(0, plan_limits['total'] - trade_counts['total'])
                }

                # Add counts to the update message
                trade_data['counts'] = {
                    "total_available": {
                        "new": trade_counts['new'],
                        "previous": trade_counts['previous'],
                        "total": trade_counts['total']
                    },
                    "shown": trade_counts,
                    "remaining": remaining,
                    "limits": {
                        "new": plan_limits.get('new'),
                        "previous": plan_limits.get('previous'),
                        "total": plan_limits.get('total')
                    }
                }
                
                await self.send(text_data=json.dumps(trade_data, cls=DecimalEncoder))
                
                # Schedule cleanup of message ID after deduplication window
                message_id = trade_data.get('trade_id')
                if message_id:
                    asyncio.create_task(self._cleanup_message_id(message_id))
        except Exception as e:
            logger.error(f"Error handling trade update: {str(e)}")
            logger.error(traceback.format_exc())

    async def _cleanup_message_id(self, message_id):
        """Clean up processed message ID after deduplication window."""
        await asyncio.sleep(self.MESSAGE_DEDUPLICATION_TIMEOUT)
        self.processed_messages.discard(message_id)

    def _is_duplicate_message(self, trade_data):
        """Check if message is a duplicate within deduplication window."""
        message_id = trade_data.get('trade_id')
        if not message_id:
            return False
            
        if message_id in self.processed_messages:
            return True
            
        self.processed_messages.add(message_id)
        return False

    async def _clear_user_cache(self):
        """Clear user's cached data."""
        cache_key = f"index_commodity_trades_{self.user.id}"
        await sync_to_async(cache.delete)(cache_key)

    async def receive(self, text_data):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(text_data)
            action = data.get('action')
            
            if action == 'refresh':
                trade_data = await self._get_filtered_trade_data(bypass_cache=True)
                await self.send(text_data=json.dumps({
                    "type": "refresh_data",
                    "stock_data": trade_data
                }, cls=DecimalEncoder))
                await self.send_success("refresh_complete")
                
        except json.JSONDecodeError:
            await self.send_error(4000, "Invalid message format")
        except Exception as e:
            logger.error(f"Error handling message: {str(e)}")
            await self.send_error(4000, str(e))