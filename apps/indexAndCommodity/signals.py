from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from decimal import Decimal
from .models import Trade, TradeHistory,Analysis,Insight
from apps.notifications.models import Notification
from django.db import DatabaseError
import logging
import traceback

logger = logging.getLogger(__name__)

class TradeUpdateBroadcaster:
    """Handles broadcasting trade updates through WebSocket."""
    
    @staticmethod
    def prepare_trade_data(trade):
        """Prepare trade data for WebSocket broadcast."""
        try:
            # Only prepare data for ACTIVE and COMPLETED trades
            if trade.status not in ['ACTIVE', 'COMPLETED']:
                logger.info(f"Skipping trade data preparation for trade ID: {trade.id} with status: {trade.status}")
                return None
                
            logger.info(f"Preparing trade data for trade ID: {trade.id}, Status: {trade.status}")
            message_type = "trade_completed" if trade.status == 'COMPLETED' else "trade_update"
            print(trade.id,'trade.index_and_commodity.id>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
            
            # Get analysis data - ensure it's never null
            analysis_data = {
                'bull_scenario': "",
                'bear_scenario': "",
                'status': 'NEUTRAL',
                'completed_at': None,
                'created_at': trade.created_at.isoformat(),
                'updated_at': trade.created_at.isoformat()
            }
            
            if hasattr(trade, 'index_and_commodity_analysis') and trade.index_and_commodity_analysis:
                analysis = trade.index_and_commodity_analysis
                analysis_data.update({
                    'bull_scenario': analysis.bull_scenario or "",
                    'bear_scenario': analysis.bear_scenario or "",
                    'status': analysis.status,
                    'completed_at': analysis.completed_at.isoformat() if analysis.completed_at else None,
                    'created_at': analysis.created_at.isoformat(),
                    'updated_at': analysis.updated_at.isoformat()
                })

            # Get trade history
            trade_history = []
            for history in trade.index_and_commodity_history.all():
                trade_history.append({
                    'buy': str(history.buy),
                    'target': str(history.target),
                    'sl': str(history.sl),
                    'timestamp': history.timestamp.isoformat(),
                    'risk_reward_ratio': str(history.risk_reward_ratio),
                    'potential_profit_percentage': str(history.potential_profit_percentage),
                    'stop_loss_percentage': str(history.stop_loss_percentage)
                })

            # Get insight data
            insight_data = None
            if hasattr(trade, 'index_and_commodity_insight') and trade.index_and_commodity_insight:
                insight = trade.index_and_commodity_insight
                insight_data = {
                    'prediction_image': insight.prediction_image.url if insight.prediction_image else None,
                    'actual_image': insight.actual_image.url if insight.actual_image else None,
                    'prediction_description': insight.prediction_description,
                    'actual_description': insight.actual_description,
                    'accuracy_score': insight.accuracy_score,
                    'analysis_result': insight.analysis_result
                }

            # Ensure warzone_history is never null
            warzone_history = trade.warzone_history or [{
                'value': float(trade.warzone),
                'changed_at': trade.created_at.isoformat()
            }]

            data = {
                "id": trade.id,
                "tradingSymbol": trade.index_and_commodity.tradingSymbol,
                "exchange": trade.index_and_commodity.exchange,
                "instrumentName": trade.index_and_commodity.instrumentName,
                "completed_trade": None,
                "intraday_trade": {
                    "id": trade.id,
                    "trade_type": trade.trade_type,
                    "status": trade.status,
                    "plan_type": trade.plan_type,
                    "warzone": str(trade.warzone),
                    "image": trade.image.url if trade.image else None,
                    "warzone_history": warzone_history,
                    "analysis": analysis_data,
                    "trade_history": trade_history,
                    "insight": insight_data,
                    "completed_at": trade.completed_at.isoformat() if trade.completed_at else None,
                    "created_at": trade.created_at.isoformat(),
                    "updated_at": trade.updated_at.isoformat()
                } if trade.trade_type == 'INTRADAY' else None,
                "positional_trade": {
                    "id": trade.id,
                    "trade_type": trade.trade_type,
                    "status": trade.status,
                    "plan_type": trade.plan_type,
                    "warzone": str(trade.warzone),
                    "image": trade.image.url if trade.image else None,
                    "warzone_history": warzone_history,
                    "analysis": analysis_data,
                    "trade_history": trade_history,
                    "insight": insight_data,
                    "completed_at": trade.completed_at.isoformat() if trade.completed_at else None,
                    "created_at": trade.created_at.isoformat(),
                    "updated_at": trade.updated_at.isoformat()
                } if trade.trade_type == 'POSITIONAL' else None,
                "created_at": trade.created_at.isoformat()
            }

            logger.info(f"Successfully prepared trade data: {data}")
            return data
        except Exception as e:
            logger.error(f"Error preparing trade data: {str(e)}")
            return None

    @staticmethod
    def broadcast_trade_update(trade):
        """Broadcast trade update through WebSocket."""
        try:
            logger.info(f"Starting trade update broadcast for trade ID: {trade.id}")
            
            # Only broadcast ACTIVE and COMPLETED trades
            if trade.status not in ['ACTIVE', 'COMPLETED']:
                logger.info(f"Skipping broadcast for trade ID: {trade.id} with status: {trade.status}")
                return
                
            channel_layer = get_channel_layer()
            if not channel_layer:
                logger.error("No channel layer available")
                return

            trade_data = TradeUpdateBroadcaster.prepare_trade_data(trade)
            if not trade_data:
                logger.error("No trade data prepared for broadcast")
                return
            
            # Get all active subscriptions that should receive this update
            from apps.subscriptions.models import Subscription
            today = timezone.now().date()
            
            subscriptions = Subscription.objects.filter(
                is_active=True,
                end_date__gte=today
            ).select_related('user')
            
            logger.info(f"Found {subscriptions.count()} active subscriptions to broadcast to")
            
            # Broadcast to each eligible user's channel
            for subscription in subscriptions:
                user_group = f"trade_updates_{subscription.user.id}"
                # logger.info(f"Broadcasting to user group: {user_group}")
                async_to_sync(channel_layer.group_send)(
                    user_group,
                    {
                        "type": "trade_update",
                        "data": trade_data
                    }
                )
            logger.info("Successfully completed broadcasting trade update")
        except Exception as e:
            logger.error(f"Error broadcasting trade update: {str(e)}")
            logger.error(traceback.format_exc())

class NotificationManager:
    @staticmethod
    def get_plan_levels(plan_type):
        """Returns list of plan levels that can access trades of given plan type"""
        plan_hierarchy = {
            'BASIC': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM'],
            'PREMIUM': ['PREMIUM', 'SUPER_PREMIUM'],
            'SUPER_PREMIUM': ['SUPER_PREMIUM'],
            'FREE_TRIAL': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM']
        }
        return plan_hierarchy.get(plan_type, [])

    @staticmethod
    def get_eligible_subscribers(plan_type):
        """Get users with active subscriptions for given plan type"""
        from apps.subscriptions.models import Subscription
        today = timezone.now().date()
        
        return Subscription.objects.filter(
            end_date__gte=today,
            is_active=True,
            plan__name__in=NotificationManager.get_plan_levels(plan_type)
        ).values_list('user_id', flat=True).distinct()

    @staticmethod
    def create_notification(trade, notification_type, short_message, detailed_message=None):
        """Create notifications for all eligible users"""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        eligible_users = User.objects.filter(
            id__in=NotificationManager.get_eligible_subscribers(trade.plan_type)
        )
        
        trade_content_type = ContentType.objects.get_for_model(Trade)
        trade_data = {
            'company': trade.index_and_commodity.tradingSymbol,
            'plan_type': trade.plan_type,
            'status': trade.status,
            'instrumentName': trade.index_and_commodity.instrumentName,
            'trade_id': str(trade.id),
            'category': 'index_and_commodity',
            'tradingSymbol': trade.index_and_commodity.tradingSymbol,
            'exchange': trade.index_and_commodity.exchange
        }
        if trade.status == 'COMPLETED':
            try:
                Notification.objects.filter(
                    trade_id=trade.id,
                    trade_status='ACTIVE'
                ).update(is_redirectable=False)
            except DatabaseError as e:
                # Log the error or handle it as needed
                print(f"Database error occurred: {e}")
        
        notifications = []
        for user in eligible_users:
            notification = Notification.objects.create(
                recipient=user,
                notification_type=notification_type,
                content_type=trade_content_type,
                object_id=trade.id,
                short_message=short_message,
                trade_status=trade.status,
                trade_id=trade.id,
                is_redirectable=True,
                detailed_message=detailed_message,
                related_url=f"/trades/{trade.id}",

                trade_data=trade_data
            )
            notifications.append(notification)
            
        return notifications

    @staticmethod
    def send_websocket_notifications(notifications):
        """Send notifications through websocket"""
        channel_layer = get_channel_layer()
        # print('send_websocket_notifications>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
        
        for notification in notifications:
            payload = {
                'type': 'new_notification',
                'message': {
                    'id': str(notification.id),
                    'type': notification.notification_type,
                    'short_message': notification.short_message,
                    'detailed_message': notification.detailed_message,
                    'trade_status':notification.trade_status,
                    'trade_id':notification.trade_id,
                    'is_redirectable':notification.is_redirectable,
                    'created_at': notification.created_at.isoformat(),
                    'related_url': notification.related_url,
                    'trade_data': notification.trade_data
                }
            }
            # print(payload,'payload>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>')
            
            async_to_sync(channel_layer.group_send)(
                f"notification_updates_{notification.recipient.id}",
                payload
            )

@receiver(post_save, sender=Trade)
def handle_trade_updates(sender, instance, created, **kwargs):
    """Handle all trade-related notifications and WebSocket updates"""
    
    logger.info(f"Trade signal received - ID: {instance.id}, Status: {instance.status}, Created: {created}")
    
    # Skip if no relevant fields changed
    if not created and not any([
        instance.tracker.has_changed('status'),
        instance.tracker.has_changed('image'),
        instance.tracker.has_changed('warzone')
    ]):
        logger.info("No relevant fields changed, skipping updates")
        return
        
    notifications = []
    
    # Only process ACTIVE and COMPLETED trades
    if instance.status not in ['ACTIVE', 'COMPLETED']:
        logger.info(f"Skipping updates for trade with status: {instance.status}")
        return
    
    # Always broadcast when a trade becomes ACTIVE or COMPLETED
    if created or instance.tracker.has_changed('status'):
        logger.info(f"Status changed or new trade created with status: {instance.status}")
        if instance.status == 'ACTIVE':
            logger.info("Trade activated, creating notification")
            notifications.extend(
                NotificationManager.create_notification(
                    instance,
                    'TRADE',
                    f"New trade activated: {instance.index_and_commodity.tradingSymbol}",
                    f"A new trade has been activated for {instance.index_and_commodity.tradingSymbol}"
                )
            )
        elif instance.status == 'COMPLETED':
            logger.info("Trade completed, creating notification")
            notifications.extend(
                NotificationManager.create_notification(
                    instance,
                    'TRADE',
                    f"Trade completed: {instance.index_and_commodity.tradingSymbol}",
                    f"Trade for {instance.index_and_commodity.tradingSymbol} has been completed"
                )
            )
            
        # Broadcast trade update through WebSocket when status changes or new trade
        logger.info("Scheduling WebSocket broadcast for status change or new trade")
        transaction.on_commit(lambda: TradeUpdateBroadcaster.broadcast_trade_update(instance))
    
    # Image update notifications (only for active trades)
    if instance.status == 'ACTIVE' and instance.tracker.has_changed('image'):
        logger.info("Image updated for active trade")
        notifications.extend(
            NotificationManager.create_notification(
                instance,
                'TRADE',
                f"Chart updated: {instance.index_and_commodity.tradingSymbol}",
                "Technical analysis chart has been updated"
            )
        )
        # Broadcast trade update for image changes
        logger.info("Scheduling WebSocket broadcast for image update")
        transaction.on_commit(lambda: TradeUpdateBroadcaster.broadcast_trade_update(instance))
    
    # Warzone update notifications (only for active trades)
    if instance.status == 'ACTIVE' and instance.tracker.has_changed('warzone'):
        logger.info("Warzone updated for active trade")
        notifications.extend(
            NotificationManager.create_notification(
                instance,
                'RISK',
                f"Risk level updated: {instance.index_and_commodity.tradingSymbol}",
                f"Risk level has changed to {instance.warzone}"
            )
        )
        # Broadcast trade update for warzone changes
        logger.info("Scheduling WebSocket broadcast for warzone update")
        transaction.on_commit(lambda: TradeUpdateBroadcaster.broadcast_trade_update(instance))
    
    # Send all notifications
    if notifications:
        logger.info(f"Scheduling {len(notifications)} notifications to be sent")
        transaction.on_commit(
            lambda: NotificationManager.send_websocket_notifications(notifications)
        )

@receiver(post_save, sender=TradeHistory)
def handle_trade_history_updates(sender, instance, created, **kwargs):
    """Handle notifications for price target updates"""
    if instance.trade.status == 'ACTIVE':
        notifications = NotificationManager.create_notification(
            instance.trade,
            'PRICE',
            f"Price targets updated: {instance.trade.index_and_commodity.tradingSymbol}",
            (f"New price targets set - Buy: {instance.buy}, "
             f"Target: {instance.target}, SL: {instance.sl}")
        )
        
        if notifications:
            transaction.on_commit(
                lambda: NotificationManager.send_websocket_notifications(notifications)
            )
            transaction.on_commit(
                lambda: TradeUpdateBroadcaster.broadcast_trade_update(instance.trade)
            )

@receiver(post_save, sender=Analysis)
def handle_analysis_updates(sender, instance, created, **kwargs):
    """Handle notifications for analysis updates"""
    if instance.trade.status == 'ACTIVE':
        action = "created" if created else "updated"
        notifications = NotificationManager.create_notification(
            instance.trade,
            'ANALYSIS',
            f"Analysis {action}: {instance.trade.index_and_commodity.tradingSymbol}",
            f"Trade analysis has been {action}"
        )
        
        if notifications:
            transaction.on_commit(
                lambda: NotificationManager.send_websocket_notifications(notifications)
            )
            transaction.on_commit(
                lambda: TradeUpdateBroadcaster.broadcast_trade_update(instance.trade)
            )

@receiver(post_save, sender=Insight)
def handle_insight_updates(sender, instance, created, **kwargs):
    """Handle notifications for insight updates"""
    if instance.trade.status == 'COMPLETED':
        action = "created" if created else "updated"
        
        # Check which fields were updated
        if not created:
            updated_fields = []
            if instance.tracker.has_changed('prediction_description'):
                updated_fields.append('prediction')
            if instance.tracker.has_changed('actual_description'):
                updated_fields.append('actual outcome')
            if instance.tracker.has_changed('accuracy_score'):
                updated_fields.append('accuracy score')
            
            if not updated_fields:
                return
                
            field_text = ", ".join(updated_fields)
        else:
            field_text = "all details"
        
        notifications = NotificationManager.create_notification(
            instance.trade,
            'INSIGHT',
            f"Trade insight {action}: {instance.trade.index_and_commodity.tradingSymbol}",
            f"Trade insight {action} with updates to {field_text}"
        )
        
        if notifications:
            transaction.on_commit(
                lambda: NotificationManager.send_websocket_notifications(notifications)
            )

@receiver(post_save, sender=Trade)
def create_trade_analysis(sender, instance, created, **kwargs):
    """Create an Analysis object when a new Trade is created"""
    if created:
        try:
            # Get analysis data from the trade if it exists
            analysis_data = getattr(instance, '_analysis_data', {})
            
            # Create analysis with default values if not provided
            Analysis.objects.create(
                trade=instance,
                bull_scenario=analysis_data.get('bull_scenario', ""),
                bear_scenario=analysis_data.get('bear_scenario', ""),
                status=analysis_data.get('status', 'NEUTRAL')
            )
            
            # Initialize warzone_history if it's empty
            if not instance.warzone_history:
                instance.warzone_history = [{
                    'value': float(instance.warzone),
                    'changed_at': instance.created_at.isoformat()
                }]
                instance.save(update_fields=['warzone_history'])
                
            logger.info(f"Created analysis and initialized warzone history for trade ID: {instance.id}")
        except Exception as e:
            logger.error(f"Error creating analysis for trade ID: {instance.id}: {str(e)}")
            logger.error(traceback.format_exc())