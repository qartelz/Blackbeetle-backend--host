from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal
from django.contrib.auth import get_user_model
from model_utils import FieldTracker

User = get_user_model()

class IndexAndCommodity(models.Model):


    class InstrumentType(models.TextChoices):
        INDEX = 'INDEX', 'Index'
        COMMODITY = 'COMMODITY', 'Commodity'
       


    tradingSymbol = models.CharField(
        max_length=50, 
        unique=True,
        help_text="Index trading symbol",
        db_index=True,
        editable=False,
        blank=False,  
        null=False 
    )
    exchange = models.CharField(
        max_length=50,
        help_text="Index exchange",
        db_index=True,
        editable=False,
        blank=False,  
        null=False 
    )
    instrumentName = models.CharField(
        max_length=50,
        choices=InstrumentType.choices,
        default=InstrumentType.INDEX,
        help_text="Instrument type",
        editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text="Index creation date")
    updated_at = models.DateTimeField(auto_now=True, help_text="Index update date")
    is_active = models.BooleanField(default=True, help_text="Index status")

    def __str__(self):
        return f"{self.tradingSymbol} {self.exchange} {self.instrumentName}"
    
    class Meta:
        verbose_name = 'Index'
        verbose_name_plural = 'Indices'
        constraints = [
            models.UniqueConstraint(
                fields=['tradingSymbol', 'exchange', 'instrumentName'],
                name='unique_index',
            )
        ]

class Trade(models.Model):
    class TradeType(models.TextChoices):
        INTRADAY = 'INTRADAY', 'Intraday'
        POSITIONAL = 'POSITIONAL', 'Positional'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        ACTIVE = 'ACTIVE', 'Active'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class PlanType(models.TextChoices):
        BASIC = 'BASIC', 'Basic'
        PREMIUM = 'PREMIUM', 'Premium'
        SUPER_PREMIUM = 'SUPER_PREMIUM', 'Super Premium'

    index_and_commodity = models.ForeignKey(
        IndexAndCommodity,
        on_delete=models.PROTECT,
        related_name='trades'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='index_and_commodity_trades'
    )
    trade_type = models.CharField(
        max_length=20,
        choices=TradeType.choices,
        db_index=True,
        help_text="Type of trade (Intraday/Positional)"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        help_text="Current status of the trade"
    )
    plan_type = models.CharField(
        max_length=15,
        choices=PlanType.choices,
        default=PlanType.BASIC,
        help_text="Subscription plan type for this trade"
    )
    warzone = models.DecimalField(
        max_digits=10,  
        decimal_places=2,
        default=0.0,
        help_text="Risk level indicator for the trade"
    )
    warzone_history = models.JSONField(
        default=list,
        blank=True,
        null=True,
        help_text="History of warzone changes"
    )
   
    image = models.ImageField(
        upload_to='trade_images/%Y/%m/',
        null=True,
        blank=True,
        help_text="Technical analysis chart or related image"
    )
    tracker = FieldTracker(fields=['status', 'image', 'warzone'])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trade was completed or cancelled"
    )

    class Meta:
        indexes = [
            models.Index(fields=['status', 'trade_type', 'plan_type']),
            models.Index(fields=['created_at', 'user']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.index_and_commodity} - {self.trade_type} - {self.status}"

    def clean(self):
        super().clean()
        
        existing_trades = Trade.objects.filter(
            index_and_commodity=self.index_and_commodity,
            status__in=[self.Status.PENDING, self.Status.ACTIVE]
        ).exclude(pk=self.pk)

        if existing_trades.exists():
            existing_trade_type = existing_trades.first().trade_type
            if existing_trade_type == self.trade_type:
                raise ValidationError(
                    f"A {self.trade_type.lower()} trade already exists for this index. "
                    f"Only one {self.trade_type.lower()} trade is allowed per index."
                )
            elif len(existing_trades) >= 2:
                raise ValidationError(
                    "Maximum limit of trades reached for this index. "
                    "Only one intraday and one positional trade are allowed."
                )

    def save(self, *args, **kwargs):
        self.clean()
        if self.status in [self.Status.COMPLETED, self.Status.CANCELLED]:
            self.completed_at = timezone.now()
        super().save(*args, **kwargs)

    @classmethod
    def get_available_trade_types(cls, index_id):
        existing_trades = cls.objects.filter(
            index_and_commodity_id=index_id,
            status__in=[cls.Status.PENDING, cls.Status.ACTIVE]
        )
        
        all_types = set(cls.TradeType.values)
        used_types = set(existing_trades.values_list('trade_type', flat=True))
        
        return list(all_types - used_types)
    
    def update_warzone(self, new_value):
        current_history = self.warzone_history or []
        
        if len(current_history) >= 20:
            current_history.pop(0)
        
        current_history.append({
            'value': float(new_value),
            'changed_at': timezone.now().isoformat()
        })
        
        self.warzone = new_value
        self.warzone_history = current_history
        self.save()

class TradeHistory(models.Model):
    trade = models.ForeignKey(
        Trade,
        on_delete=models.CASCADE,
        related_name='index_and_commodity_history'
    )
    buy = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text="Entry price point"
    )
    target = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        help_text="Target price for the trade"
    )
    sl = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        verbose_name="Stop Loss",
        help_text="Stop loss price point"
    )
    tracker = FieldTracker(fields=['buy', 'target', 'sl'])
    timestamp = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Trade histories"
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['trade', 'timestamp']),
        ]
        get_latest_by = 'timestamp'

    def __str__(self):
        return f"{self.trade} - {self.timestamp}"

    def clean(self):
        super().clean()
        if self.sl >= self.buy:
            raise ValidationError("Stop loss must be lower than buy price")
        if self.target <= self.buy:
            raise ValidationError("Target must be higher than buy price")

    @property
    def risk_reward_ratio(self):
        potential_profit = abs(self.target - self.buy)
        potential_loss = abs(self.buy - self.sl)
        return round(potential_profit / potential_loss, 2) if potential_loss else None

    @property
    def potential_profit_percentage(self):
        return round(((self.target - self.buy) / self.buy * 100), 2) if self.buy else None

    @property
    def stop_loss_percentage(self):
        return round(((self.buy - self.sl) / self.buy * 100), 2) if self.buy else None

class Analysis(models.Model):
    class Sentiment(models.TextChoices):
        BEARISH = 'BEARISH', 'Bearish'
        BULLISH = 'BULLISH', 'Bullish'
        NEUTRAL = 'NEUTRAL', 'Neutral'

    trade = models.OneToOneField(
        Trade,
        on_delete=models.CASCADE,
        related_name='index_and_commodity_analysis'
    )
    bull_scenario = models.TextField(
        help_text="Analysis of potential bullish outcomes",
        null=True,
        blank=True
    )
    bear_scenario = models.TextField(
        help_text="Analysis of potential bearish outcomes",
        null=True,
        blank=True
    )
    status = models.CharField(
        max_length=20,
        choices=Sentiment.choices,
        default=Sentiment.NEUTRAL,
        help_text="Current market sentiment"
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the analysis was completed"
    )
    tracker = FieldTracker(fields=['bull_scenario', 'bear_scenario', 'status'])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Analyses"

    def __str__(self):
        return f"Analysis for {self.trade}"

    def mark_complete(self):
        self.completed_at = timezone.now()
        self.save()

class Insight(models.Model):
    class AnalysisSection(models.TextChoices):
        OVERVIEW = 'OVERVIEW', 'Overview'
        TECHNICAL = 'TECHNICAL', 'Technical Analysis'
        FUNDAMENTAL = 'FUNDAMENTAL', 'Fundamental Analysis'
        RISK = 'RISK', 'Risk Analysis'
        RECOMMENDATION = 'RECOMMENDATION', 'Recommendation'
        OTHER = 'OTHER', 'Other Insights'

    class ParagraphType(models.TextChoices):
        SUMMARY = 'SUMMARY', 'Summary'
        KEY_POINTS = 'KEY_POINTS', 'Key Points'
        DETAILS = 'DETAILS', 'Detailed Analysis'
        CONCLUSION = 'CONCLUSION', 'Conclusion'

    trade = models.OneToOneField(
        Trade,
        on_delete=models.CASCADE,
        related_name='index_and_commodity_insight'
    )
    prediction_image = models.ImageField(
        upload_to='trade_insights/predictions/',
        help_text="Technical analysis prediction chart",
        null=True,
        blank=True
    )
    actual_image = models.ImageField(
        upload_to='trade_insights/actuals/',
        null=True,
        blank=True,
        help_text="Actual outcome chart"
    )
    prediction_description = models.TextField(
        help_text="Detailed description of the predicted outcome",
        null=True,
        blank=True

    )
    actual_description = models.TextField(
        null=True,
        blank=True,
        help_text="Description of the actual outcome"
    )
    accuracy_score = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage accuracy of the prediction"
    )
    analysis_result = models.JSONField(
        null=True,
        blank=True,
        help_text="Structured analysis result with sections and paragraphs",
        default=dict
    )
    tracker = FieldTracker(fields=['prediction_description', 'actual_description', 'accuracy_score'])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Insight for {self.trade}"

    def clean(self):
        super().clean()
        
        if self.trade.status != Trade.Status.COMPLETED:
            raise ValidationError(
                "Prediction vs Actual analysis Result will be updated once the trade has expired. "
                f"Current trade status: {self.trade.get_status_display()}"
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @property
    def is_accurate(self):
        return self.accuracy_score and self.accuracy_score >= 80

    def add_paragraph(self, section_type, paragraph_type, content, order=None):
        if not self.analysis_result:
            self.analysis_result = {'sections': {}}

        if section_type not in self.AnalysisSection.values:
            raise ValidationError(f"Invalid section type: {section_type}")

        if paragraph_type not in self.ParagraphType.values:
            raise ValidationError(f"Invalid paragraph type: {paragraph_type}")

        if section_type not in self.analysis_result['sections']:
            self.analysis_result['sections'][section_type] = {
                'paragraphs': [],
                'last_updated': timezone.now().isoformat()
            }

        paragraph_data = {
            'type': paragraph_type,
            'content': content,
            'order': order if order is not None else len(self.analysis_result['sections'][section_type]['paragraphs']) + 1,
            'last_updated': timezone.now().isoformat()
        }

        section = self.analysis_result['sections'][section_type]
        updated = False
        for i, para in enumerate(section['paragraphs']):
            if para['type'] == paragraph_type:
                section['paragraphs'][i] = paragraph_data
                updated = True
                break

        if not updated:
            section['paragraphs'].append(paragraph_data)

        section['paragraphs'].sort(key=lambda x: x['order'])
        section['last_updated'] = timezone.now().isoformat()
        self.save()

    def get_section_paragraphs(self, section_type):
        if not self.analysis_result or 'sections' not in self.analysis_result:
            return None
            
        section = self.analysis_result['sections'].get(section_type)
        return section['paragraphs'] if section else None

    def get_paragraph(self, section_type, paragraph_type):
        paragraphs = self.get_section_paragraphs(section_type)
        if not paragraphs:
            return None
            
        for para in paragraphs:
            if para['type'] == paragraph_type:
                return para['content']
        return None

    def get_formatted_analysis(self, include_metadata=False):
        if not self.analysis_result or 'sections' not in self.analysis_result:
            return "No analysis available"

        formatted_text = []
        for section_type, section_data in self.analysis_result['sections'].items():
            section_title = dict(self.AnalysisSection.choices)[section_type]
            formatted_text.append(f"\n## {section_title}")
            
            if include_metadata:
                formatted_text.append(f"Last Updated: {section_data['last_updated']}\n")

            for para in section_data['paragraphs']:
                para_title = dict(self.ParagraphType.choices)[para['type']]
                formatted_text.append(f"### {para_title}")
                formatted_text.append(para['content'])
                formatted_text.append("")  # Empty line between paragraphs

        return "\n".join(formatted_text)

    def calculate_accuracy(self, prediction_data, actual_data):
        """
        Calculate prediction accuracy based on various metrics.
        
        Args:
            prediction_data (dict): Predicted trade outcomes
            actual_data (dict): Actual trade outcomes
        
        Returns:
            float: Accuracy score between 0-100
        """
        if not prediction_data or not actual_data:
            return 0.0

        # Define accuracy calculation logic
        accuracy_components = []

        # Price movement accuracy
        predicted_direction = prediction_data.get('price_direction')
        actual_direction = actual_data.get('price_direction')
        direction_match = predicted_direction == actual_direction
        accuracy_components.append(100 if direction_match else 0)

        # Target price proximity
        predicted_target = prediction_data.get('target_price')
        actual_target = actual_data.get('actual_price')
        if predicted_target and actual_target:
            proximity_score = max(0, 100 - abs((predicted_target - actual_target) / actual_target * 100))
            accuracy_components.append(proximity_score)

        # Risk management accuracy
        predicted_risk = prediction_data.get('risk_level')
        actual_risk = actual_data.get('realized_risk')
        risk_match = abs(predicted_risk - actual_risk) <= 0.2
        accuracy_components.append(100 if risk_match else 0)

        # Calculate final accuracy
        final_accuracy = sum(accuracy_components) / len(accuracy_components)
        self.accuracy_score = round(final_accuracy, 2)
        self.save()

        return self.accuracy_score

    def generate_comprehensive_report(self):
        """
        Generate a comprehensive trade performance report.
        
        Returns:
            dict: Detailed trade performance insights
        """
        report = {
            'trade_summary': {
                'symbol': self.trade.index_and_commodity.tradingSymbol,
                'trade_type': self.trade.trade_type,
                'trade_duration': self.trade.completed_at - self.trade.created_at
            },
            'prediction_analysis': {
                'accuracy_score': self.accuracy_score,
                'key_predictions': self.get_paragraph(
                    self.AnalysisSection.RECOMMENDATION, 
                    self.ParagraphType.KEY_POINTS
                )
            },
            'performance_metrics': {
                'risk_reward_ratio': self.trade.tradehistory_set.first().risk_reward_ratio,
                'profit_percentage': self.trade.tradehistory_set.first().potential_profit_percentage
            },
            'visual_analysis': {
                'prediction_chart': self.prediction_image.url if self.prediction_image else None,
                'actual_chart': self.actual_image.url if self.actual_image else None
            }
        }
        
        return report