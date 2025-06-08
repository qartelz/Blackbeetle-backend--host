from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.cache import cache
from .models import Trade, Analysis, TradeHistory, Insight
from apps.subscriptions.models import Subscription
import json
import logging
import asyncio
from decimal import Decimal
from urllib.parse import parse_qs
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.db import transaction
from typing import Dict, List, Optional, Set
import traceback

logger = logging.getLogger(__name__)
User = get_user_model()

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
            return await database_sync_to_async(cache.get)(cache_key)
        except Exception as e:
            logger.error(f"Failed to get cached trades: {str(e)}")
            return None

    @staticmethod
    async def set_cached_trades(cache_key: str, data: Dict):
        """Store trade data in the cache asynchronously."""
        try:
            await database_sync_to_async(cache.set)(cache_key, data, IndexAndCommodityUpdateManager.CACHE_TIMEOUT)
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
        """Handle WebSocket connection."""
        try:
            self.user = self.scope["user"]
            if not self.user.is_authenticated:
                logger.error("Unauthenticated user tried to connect")
                await self.close()
                return

            # Get user's subscription
            self.subscription = await self._get_active_subscription()
            if not self.subscription:
                logger.error(f"No active subscription found for user {self.user.id}")
                await self.close()
                return

            # Join user's trade updates group
            self.user_group = f"trade_updates_{self.user.id}"
            await self.channel_layer.group_add(self.user_group, self.channel_name)
            await self.accept()
            logger.info(f"WebSocket connection accepted for user {self.user.id}")

            # Send initial trade data
            await self.send_initial_trades()
            logger.info(f"Initial trade data sent to user {self.user.id}")

        except Exception as e:
            logger.error(f"Error in WebSocket connection: {str(e)}")
            logger.error(traceback.format_exc())
            await self.close()

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        if self.user_group:
            await self.channel_layer.group_discard(self.user_group, self.channel_name)
        if self._initial_data_task:
            self._initial_data_task.cancel()

    async def trade_update(self, event):
        """Handle trade update messages."""
        try:
            # Get the trade object and format it
            trade_id = event["data"].get("id")
            print(trade_id,'trade_id>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
            if not trade_id:
                logger.error("No trade ID provided in update event")
                return

            trade = await database_sync_to_async(Trade.objects.select_related(
                'index_and_commodity',
                'index_and_commodity_analysis',
                'index_and_commodity_insight'
            ).prefetch_related(
                'index_and_commodity_history'
            ).get)(id=trade_id)
            print(trade,'trade>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
            formatted_trade = await database_sync_to_async(self._format_trade)(trade)
            print(formatted_trade,'formatted_trade>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
            if not formatted_trade:
                logger.error(f"Failed to format trade {trade_id}")
                return

            response_data = {
                "type": "trade_update",
                "data": {
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [formatted_trade]
                }
            }
            
            await self.send(text_data=json.dumps(response_data, cls=DecimalEncoder))
            logger.info(f"Trade update sent to user {self.user.id}")
        except Exception as e:
            logger.error(f"Error sending trade update: {str(e)}")
            logger.error(traceback.format_exc())

    @database_sync_to_async
    def _get_active_subscription(self):
        """Get user's active subscription."""
        return Subscription.objects.filter(
            user=self.user,
            is_active=True
        ).first()

    # async def send_initial_trades(self):
    #     """Send initial trade data to the client."""
    #     try:
    #         trades = await self._get_active_trades()
    #         formatted_trades = []
            
    #         for trade in trades:
    #             trade_data = await database_sync_to_async(self._format_trade)(trade)
    #             if trade_data:
    #                 formatted_trades.append(trade_data)
            
    #         response_data = {
    #             "type": "initial_trades_index_and_commodity",
    #             "data": {
    #                 "count": len(formatted_trades),
    #                 "next": None,
    #                 "previous": None,
    #                 "results": formatted_trades
    #             }
    #         }
            
    #         await self.send(text_data=json.dumps(response_data, cls=DecimalEncoder))
    #         logger.info(f"Initial trades sent to user {self.user.id}")
    #     except Exception as e:
    #         logger.error(f"Error sending initial trades: {str(e)}")
    #         logger.error(traceback.format_exc())
    async def send_initial_trades(self):
        """Send initial trade data to the client with combined trades by symbol."""
        try:
            trades = await self._get_active_trades()
            
            # Group trades by trading symbol and exchange
            trades_by_symbol = {}
            
            for trade in trades:
                key = f"{trade.index_and_commodity.tradingSymbol}_{trade.index_and_commodity.exchange}"
                if key not in trades_by_symbol:
                    trades_by_symbol[key] = []
                trades_by_symbol[key].append(trade)
            
            formatted_trades = []
            
            for symbol_trades in trades_by_symbol.values():
                combined_trade = await database_sync_to_async(self._combine_trades)(symbol_trades)
                if combined_trade:
                    formatted_trades.append(combined_trade)
            
            response_data = {
                "type": "initial_trades_index_and_commodity",
                "data": {
                    "count": len(formatted_trades),
                    "next": None,
                    "previous": None,
                    "results": formatted_trades
                }
            }
            
            await self.send(text_data=json.dumps(response_data, cls=DecimalEncoder))
            logger.info(f"Initial trades sent to user {self.user.id}")
        except Exception as e:
            logger.error(f"Error sending initial trades: {str(e)}")
            logger.error(traceback.format_exc())

    def _combine_trades(self, trades):
        """Combine multiple trades for the same symbol into a single response."""
        if not trades:
            return None
        
        # Use the first trade as base for common fields
        base_trade = trades[0]
        
        # Find positional and intraday trades by checking trade_type
        positional_trade = None
        intraday_trade = None
        
        for trade in trades:
            if trade.trade_type == 'POSITIONAL':
                positional_trade = trade
            elif trade.trade_type == 'INTRADAY':
                intraday_trade = trade
        
        # Use the earliest creation date
        earliest_created_at = min(trade.created_at for trade in trades)
        
        # Format the combined trade
        trade_data = {
            "id": base_trade.id,
            "tradingSymbol": base_trade.index_and_commodity.tradingSymbol,
            "exchange": base_trade.index_and_commodity.exchange,
            "instrumentName": base_trade.index_and_commodity.instrumentName,
            "completed_trade": None,  # Handle completed trades if needed
            "intraday_trade": self._format_trade_details(intraday_trade) if intraday_trade else None,
            "positional_trade": self._format_trade_details(positional_trade) if positional_trade else None,
            "created_at": earliest_created_at.isoformat()
        }
        
        return trade_data

    def _format_trade_details(self, trade):
        """Format individual trade details for either positional or intraday."""
        if not trade:
            return None
        
        try:
            # Base trade details
            trade_details = {
                "id": trade.id,
                "trade_type": trade.trade_type,
                "status": trade.status,
                "plan_type": trade.plan_type,
                "warzone": str(trade.warzone),
                "image": trade.image.url if trade.image else None,
                "warzone_history": trade.warzone_history or [],
                "analysis": None,
                "trade_history": [],
                "insight": None,
                "completed_at": trade.completed_at.isoformat() if trade.completed_at else None,
                "created_at": trade.created_at.isoformat(),
                "updated_at": trade.updated_at.isoformat()
            }

            # Get and format analysis data
            if hasattr(trade, 'index_and_commodity_analysis') and trade.index_and_commodity_analysis:
                analysis = trade.index_and_commodity_analysis
                trade_details["analysis"] = {
                    'bull_scenario': analysis.bull_scenario or "",
                    'bear_scenario': analysis.bear_scenario or "",
                    'status': analysis.status,
                    'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None,
                    'created_at': analysis.created_at.isoformat(),
                    'updated_at': analysis.updated_at.isoformat()
                }

            # Get and format trade history
            if hasattr(trade, 'index_and_commodity_history'):
                histories = trade.index_and_commodity_history.all()
                trade_details["trade_history"] = [
                    {
                        'buy': str(history.buy),
                        'target': str(history.target),
                        'sl': str(history.sl),
                        'timestamp': history.timestamp.isoformat(),
                        'risk_reward_ratio': str(history.risk_reward_ratio),
                        'potential_profit_percentage': str(history.potential_profit_percentage),
                        'stop_loss_percentage': str(history.stop_loss_percentage)
                    }
                    for history in histories
                ]

            # Get and format insight data
            if hasattr(trade, 'index_and_commodity_insight') and trade.index_and_commodity_insight:
                insight = trade.index_and_commodity_insight
                trade_details["insight"] = {
                    'prediction_image': insight.prediction_image.url if insight.prediction_image else None,
                    'actual_image': insight.actual_image.url if insight.actual_image else None,
                    'prediction_description': insight.prediction_description,
                    'actual_description': insight.actual_description,
                    'accuracy_score': insight.accuracy_score,
                    'analysis_result': insight.analysis_result
                }

            return trade_details

        except Exception as e:
            logger.error(f"Error formatting trade details: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    @database_sync_to_async
    def _get_active_trades(self):
        """Get all active trades."""
        return list(Trade.objects.filter(
            status='ACTIVE'
        ).select_related(
            'index_and_commodity',
            'index_and_commodity_analysis',
            'index_and_commodity_insight'
        ).prefetch_related(
            'index_and_commodity_history'
        ))

    def _format_trade(self, trade):
        """Format trade data for sending to client."""
        try:
            # Get the index and commodity data
            index_data = {
                "id": trade.id,
                "tradingSymbol": trade.index_and_commodity.tradingSymbol,
                "exchange": trade.index_and_commodity.exchange,
                "instrumentName": trade.index_and_commodity.instrumentName,
                "completed_trade": None
            }

            # Format trade details
            trade_details = {
                "id": trade.id,
                "trade_type": trade.trade_type,
                "status": trade.status,
                "plan_type": trade.plan_type,
                "warzone": str(trade.warzone),
                "image": trade.image.url if trade.image else None,
                "warzone_history": trade.warzone_history or [],
                "analysis": None,  # Will be updated below
                "trade_history": [],  # Will be updated below
                "insight": None,  # Will be updated below
                "completed_at": trade.completed_at.isoformat() if trade.completed_at else None,
                "created_at": trade.created_at.isoformat(),
                "updated_at": trade.updated_at.isoformat()
            }

            # Get and format analysis data
            if hasattr(trade, 'index_and_commodity_analysis'):
                analysis = trade.index_and_commodity_analysis
                if analysis:
                    trade_details["analysis"] = {
                        'bull_scenario': analysis.bull_scenario or "",
                        'bear_scenario': analysis.bear_scenario or "",
                        'status': analysis.status,
                        'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None,
                        'created_at': analysis.created_at.isoformat(),
                        'updated_at': analysis.updated_at.isoformat()
                    }

            # Get and format trade history
            if hasattr(trade, 'index_and_commodity_history'):
                histories = trade.index_and_commodity_history.all()
                trade_details["trade_history"] = [
                    {
                        'buy': str(history.buy),
                        'target': str(history.target),
                        'sl': str(history.sl),
                        'timestamp': history.timestamp.isoformat(),
                        'risk_reward_ratio': str(history.risk_reward_ratio),
                        'potential_profit_percentage': str(history.potential_profit_percentage),
                        'stop_loss_percentage': str(history.stop_loss_percentage)
                    }
                    for history in histories
                ]

            # Get and format insight data
            if hasattr(trade, 'index_and_commodity_insight'):
                insight = trade.index_and_commodity_insight
                if insight:
                    trade_details["insight"] = {
                        'prediction_image': insight.prediction_image.url if insight.prediction_image else None,
                        'actual_image': insight.actual_image.url if insight.actual_image else None,
                        'prediction_description': insight.prediction_description,
                        'actual_description': insight.actual_description,
                        'accuracy_score': insight.accuracy_score,
                        'analysis_result': insight.analysis_result
                    }

            # Add trade details to appropriate field based on trade type
            if trade.trade_type == 'INTRADAY':
                index_data["intraday_trade"] = trade_details
                index_data["positional_trade"] = None
            else:
                index_data["intraday_trade"] = None
                index_data["positional_trade"] = trade_details

            index_data["created_at"] = trade.created_at.isoformat()
            
            return index_data

        except Exception as e:
            logger.error(f"Error formatting trade data: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    @database_sync_to_async
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

    @database_sync_to_async
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
            cached_data = await database_sync_to_async(cache.get)(cache_key)
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

        await database_sync_to_async(cache.set)(cache_key, response_data, self.cache_timeout)
        return response_data

    async def send_initial_data(self):
        """Send initial trade data to client."""
        try:
            response_data = await self._get_filtered_trade_data()
            await self.send(text_data=json.dumps(response_data, cls=DecimalEncoder))
            await self.send_success("initial_data")
        except Exception as e:
            logger.error(f"Error sending initial data: {str(e)}")
            await self.send_error(4006)

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