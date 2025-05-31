from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from ..models import Trade, Analysis, IndexAndCommodity
from ..serializers.trade_related_serializers import TradeSerializer, AnalysisSerializer
from ..serializers.grouped_trades_serializers import TradeDetailSerializer
from rest_framework.pagination import PageNumberPagination
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError 
from ..Filter.index_and_commodity_filter import TradeFilter
from django.db.models import Q
import logging
import traceback

logger = logging.getLogger(__name__)
User = get_user_model()

class CustomTradePagination(PageNumberPagination):
    page_size = 20  # Changed from 100 to 20 as default
    page_size_query_param = 'page_size'
    max_page_size = 100

class TradeViewSet(viewsets.ModelViewSet):
    """
    Complete Trade Management System with Flexible Analysis
    """
    queryset = Trade.objects.select_related(
        'index_and_commodity',
        'index_and_commodity_analysis'
    ).prefetch_related(
        'index_and_commodity_history',
        'index_and_commodity_insight'
    )
    serializer_class = TradeSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CustomTradePagination
    parser_classes = (MultiPartParser, FormParser, JSONParser)
    filter_backends = [DjangoFilterBackend]
    filterset_class = TradeFilter

    def get_queryset(self):
        queryset = self.queryset
        
        # Apply status filter if provided
        status = self.request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        else:
            # If no status filter, show all except cancelled
            queryset = queryset.exclude(status='CANCELLED')
            
        # Apply trade type filter if provided
        trade_type = self.request.query_params.get('trade_type')
        if trade_type:
            queryset = queryset.filter(trade_type=trade_type)
            
        # Apply plan type filter if provided
        plan_type = self.request.query_params.get('plan_type')
        if plan_type:
            queryset = queryset.filter(plan_type=plan_type)
            
        # Apply exchange filter if provided
        exchange = self.request.query_params.get('exchange')
        if exchange:
            queryset = queryset.filter(index_and_commodity__exchange=exchange)
            
        # Apply search filter if provided
        search = self.request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(index_and_commodity__tradingSymbol__icontains=search) |
                Q(index_and_commodity__exchange__icontains=search)
            )
            
        return queryset.order_by('-created_at')

    def create(self, request, *args, **kwargs):
        try:
            # Extract analysis data if provided
            analysis_data = request.data.pop('analysis', {})
            
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            # Run model validation
            instance = serializer.save()
            
            # Attach analysis data to be used by the signal
            instance._analysis_data = analysis_data
            
            try:
                instance.clean()
            except ValidationError as e:
                instance.delete()
                return Response(
                    {"error": str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
            # Return the trade data in the consistent format
            trade_data = TradeDetailSerializer(instance).data
            return Response({
                "count": 1,
                "next": None,
                "previous": None,
                "results": [trade_data]
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    def update(self, request, *args, **kwargs):
        try:
            instance = self.get_object()
            
            # Check if user owns the trade
            if instance.user != request.user:
                return Response(
                    {"error": "You don't have permission to modify this trade"},
                    status=status.HTTP_403_FORBIDDEN
                )
                
            serializer = self.get_serializer(instance, data=request.data, partial=kwargs.pop('partial', False))
            serializer.is_valid(raise_exception=True)
            
            try:
                updated_instance = serializer.save()
                updated_instance.clean()
            except ValidationError as e:
                return Response(
                    {"error": str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
            return Response(serializer.data)
        except Exception as e:
            return Response(
                {"error": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

    @action(detail=True, methods=['PATCH'], url_path='update-analysis')
    def update_analysis(self, request, pk=None):
        """
        Flexible analysis update with all fields optional
        Example Requests:
        1. Update status only:
        {"analysis": {"status": "BULLISH"}}
        
        2. Add bear scenario:
        {"analysis": {"bear_scenario": "Market downturn expected"}}
        
        3. Clear bull scenario:
        {"analysis": {"bull_scenario": ""}}
        """
        trade = self.get_object()
        
        if trade.status == Trade.Status.COMPLETED:
            return Response(
                {"error": "Cannot update analysis for completed trades"},
                status=status.HTTP_400_BAD_REQUEST
            )

        analysis_data = request.data.get('analysis', {})
        analysis, _ = Analysis.objects.get_or_create(trade=trade)
        
        serializer = AnalysisSerializer(
            analysis, 
            data=analysis_data, 
            partial=True,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        updated_analysis = serializer.save()
        
        # Update completion timestamp if status changes from neutral
        if updated_analysis.status != Analysis.Sentiment.NEUTRAL:
            if not updated_analysis.completed_at:
                updated_analysis.completed_at = timezone.now()
                updated_analysis.save()

        return Response({
            "status": "success",
            "trade_id": trade.id,
            "analysis": serializer.data
        })

    @action(detail=True, methods=['PATCH'], url_path='update-image')
    def update_image(self, request, pk=None):
        """Update trade technical analysis image"""
        trade = self.get_object()
        
        if 'image' not in request.FILES:
            return Response(
                {"error": "Image file required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Delete old image if exists
        if trade.image:
            trade.image.delete(save=False)
        
        trade.image = request.FILES['image']
        trade.save()
        
        return Response({
            "status": "success",
            "image_url": trade.image.url
        })

    @action(detail=True, methods=['PATCH'], url_path='update-warzone')
    def update_warzone(self, request, pk=None):
        """Update risk level """
        trade = self.get_object()
        new_value = request.data.get('warzone')
        
        try:
            new_value = float(new_value)
            if not (0 <= new_value):
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"error": "Warzone must be a number greater than or equal to 0"},
                status=status.HTTP_400_BAD_REQUEST
            )

        trade.update_warzone(new_value)
        return Response({
            "status": "success",
            "new_warzone": new_value,
            "history": trade.warzone_history
        })

    @action(detail=True, methods=['PATCH'], url_path='update-status')
    def update_status(self, request, pk=None):
        """Update trade status with validation"""
        try:
            trade = self.get_object()
            new_status = request.data.get('status')

            if new_status not in Trade.Status.values:
                return Response(
                    {
                        "status": "error",
                        "message": f"Invalid status. Valid options: {Trade.Status.values}"
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            if trade.status == Trade.Status.COMPLETED:
                return Response(
                    {
                        "status": "error",
                        "message": "Completed trades cannot be modified"
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            old_status = trade.status
            trade.status = new_status
            
            if new_status in [Trade.Status.COMPLETED, Trade.Status.CANCELLED]:
                trade.completed_at = timezone.now()
            
            try:
                # Run model validation
                trade.clean()
                trade.save()
                
                # Return the updated trade data in the same format as other responses
                trade_data = TradeDetailSerializer(trade).data
                return Response({
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [trade_data]
                })
                
            except ValidationError as e:
                # Restore the old status if validation fails
                trade.status = old_status
                trade.completed_at = None if old_status not in [Trade.Status.COMPLETED, Trade.Status.CANCELLED] else trade.completed_at
                trade.save()
                
                return Response(
                    {
                        "status": "error",
                        "message": str(e.messages[0] if isinstance(e.messages, list) else e.messages)
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        except Exception as e:
            logger.error(f"Error updating trade status: {str(e)}")
            logger.error(traceback.format_exc())
            return Response(
                {
                    "status": "error",
                    "message": "An unexpected error occurred while updating trade status"
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=False, methods=['GET'], url_path='available-trades')
    def available_trades(self, request):
        """Get available trade types for specific index"""
        index_id = request.query_params.get('index_id')
        if not index_id:
            return Response(
                {"error": "Missing index_id query parameter"},
                status=status.HTTP_400_BAD_REQUEST
            )

        index = get_object_or_404(IndexAndCommodity, id=index_id)
        available_types = Trade.get_available_trade_types(index.id)
        return Response({
            "index": index.tradingSymbol,
            "available_types": available_types
        })

    @action(detail=False, methods=['GET'], url_path='grouped-trades')
    def grouped_trades(self, request):
        """Get trades grouped by index with type separation"""
        trades = self.filter_queryset(self.get_queryset())
        grouped = {}
        
        for trade in trades:
            symbol = trade.index_and_commodity.tradingSymbol
            if symbol not in grouped:
                grouped[symbol] = {
                    'intraday': None,
                    'positional': None,
                    'index_id': trade.index_and_commodity.id
                }
            grouped[symbol][trade.trade_type.lower()] = TradeDetailSerializer(trade).data
        
        return Response(grouped)

    def destroy(self, request, *args, **kwargs):
        """Cancel trade (soft delete)"""
        trade = self.get_object()
        
        if trade.status == Trade.Status.COMPLETED:
            return Response(
                {"error": "Cannot delete completed trades"},
                status=status.HTTP_400_BAD_REQUEST
            )

        trade.status = Trade.Status.CANCELLED
        trade.completed_at = timezone.now()
        trade.save()
        
        return Response(status=status.HTTP_204_NO_CONTENT)

