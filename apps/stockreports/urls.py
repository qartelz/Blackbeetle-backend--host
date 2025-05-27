from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from apps.stockreports.views import proxy_pdf

router = DefaultRouter()
router.register(r'reports', views.StockReportViewSet, basename='stockreport')

urlpatterns = [
    # ViewSet URLs
    path('', include(router.urls)),
    
    # Additional URLs for generic views
    path('list/', views.StockReportListView.as_view(), name='stockreport-list'),
    path('create/', views.StockReportCreateView.as_view(), name='stockreport-create'),
    path('detail/<int:pk>/', views.StockReportDetailView.as_view(), name='stockreport-detail'),
    path('update/<int:pk>/', views.StockReportUpdateView.as_view(), name='stockreport-update'),
    path('delete/<int:pk>/', views.StockReportDeleteView.as_view(), name='stockreport-delete'),
    path('proxy/pdf/', proxy_pdf, name='proxy_pdf'),
]