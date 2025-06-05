from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django_filters import rest_framework as filters
from django.db.models import Q
from collections import defaultdict

from ..models import Trade, IndexAndCommodity
# from ..Filter.GroupedTradeFilter import GroupedTradeFilter



from ..serializers.grouped_trades_serializers import IndexAndCommodityTradesSerializer

from django.utils import timezone


class GroupedTradeFilter(filters.FilterSet):
    status = filters.ChoiceFilter(
        choices=[('ACTIVE', 'Active')],
        method='filter_status'
    )
    instrumentName = filters.ChoiceFilter(
        choices=IndexAndCommodity.InstrumentType.choices
    )
    tradingSymbol = filters.CharFilter(field_name='tradingSymbol', lookup_expr='icontains')

    class Meta:
        model = IndexAndCommodity
        fields = ['status', 'instrumentName', 'tradingSymbol']
    
    def filter_status(self, queryset, name, value):
        if value:
            return queryset.filter(
                trades__status=value
            ).distinct()
        return queryset



class GroupedTradeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = IndexAndCommodityTradesSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = (filters.DjangoFilterBackend,)
    filterset_class = GroupedTradeFilter
    
#     def get_queryset(self):
#         user = self.request.user
#         current_date = timezone.now()
        
#         # Get indices that have active or completed trades
#         base_queryset = IndexAndCommodity.objects.filter(
#             trades__status__in=['ACTIVE']
#         ).distinct()
        
#         if user.is_staff:
#             return base_queryset
#         print(user, "user................................") 

#         current_subscription = user.subscriptions.filter(
#             is_active=True,
#             start_date__lte=current_date,
#             end_date__gte=current_date
#         ).first()
#         print(current_subscription , "current_subscription................................")
#         print("Subscription Dates:", current_subscription.start_date, current_subscription.end_date)
#         if not current_subscription:
#             raise PermissionDenied(
#                 detail="You don't have an active subscription plan. "
#                 "Please subscribe to access trade information."
#             )

#         # Get trades within the subscription period and plan type
#         plan_type = current_subscription.plan.name
#         plan_filters = {
#             'BASIC': ['BASIC'],
#             'PREMIUM': ['BASIC', 'PREMIUM'],
#             'SUPER_PREMIUM': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM'],
#             'FREE_TRIAL': ['BASIC', 'PREMIUM', 'SUPER_PREMIUM']
#         }
        
#         allowed_plans = plan_filters.get(plan_type, [])
#         print(allowed_plans, "allowed_plans................................")
        
#         return base_queryset.filter(
#             trades__created_at__date__gte=current_subscription.start_date,
#             trades__created_at__date__lte=current_subscription.end_date,
#             trades__plan_type__in=allowed_plans
#         ).distinct()
    def get_queryset(self):
        user = self.request.user
        current_date = timezone.now()
        
        # Initial base queryset: All active trades
        base_queryset = IndexAndCommodity.objects.filter(
            trades__status='ACTIVE'
        ).distinct()

        if user.is_staff:
            return base_queryset

        current_subscription = user.subscriptions.filter(
            is_active=True,
            start_date__lte=current_date,
            end_date__gte=current_date
        ).first()

        if not current_subscription:
            raise PermissionDenied(
                detail="You don't have an active subscription plan. "
                    "Please subscribe to access trade information."
            )

        # Just filter based on query params: instrumentName and tradingSymbol
        instrument_name = self.request.query_params.get('instrumentName')
        trading_symbol = self.request.query_params.get('tradingSymbol')

        if instrument_name:
            base_queryset = base_queryset.filter(instrumentName=instrument_name)
        if trading_symbol:
            base_queryset = base_queryset.filter(tradingSymbol__icontains=trading_symbol)

        return base_queryset
