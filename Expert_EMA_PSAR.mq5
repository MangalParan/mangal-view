//+------------------------------------------------------------------+
//|                                                 ExpertMAPSAR.mq5 |
//|                             Copyright 2000-2026, MetaQuotes Ltd. |
//|                                                     www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "Copyright 2000-2026, MetaQuotes Ltd."
#property link      "https://www.mql5.com"
#property version   "1.00"
//+------------------------------------------------------------------+
//| Include                                                          |
//+------------------------------------------------------------------+
#include <Expert\Expert.mqh>
#include <Expert\ExpertSignal.mqh>
#include <Expert\Trailing\TrailingParabolicSAR.mqh>
#include <Expert\Money\MoneyFixedLot.mqh>
//+------------------------------------------------------------------+
//| Inputs                                                           |
//+------------------------------------------------------------------+
//--- inputs for expert
input string             Inp_Expert_Title                 ="ExpertMAPSAR";
int                      Expert_MagicNumber               =14598;
bool                     Expert_EveryTick                 =false;
//--- inputs for signal
input int                Inp_Signal_EMA_Fast_Period       =5;
input int                Inp_Signal_EMA_Slow_Period       =13;
input ENUM_APPLIED_PRICE Inp_Signal_EMA_Applied           =PRICE_CLOSE;
//--- inputs for trailing
input double             Inp_Trailing_ParabolicSAR_Step   =0.02;
input double             Inp_Trailing_ParabolicSAR_Maximum=0.2;
//--- inputs for money
input double             Inp_Money_FixedLot_Lots          =0.1;
//+------------------------------------------------------------------+
//| Class CSignalEMACross.                                           |
//| Purpose: Trade signal generator based on the crossover of a      |
//|          fast and a slow Exponential Moving Average.             |
//| Is derived from the CExpertSignal class.                         |
//+------------------------------------------------------------------+
class CSignalEMACross : public CExpertSignal
  {
protected:
   CiMA              m_ma_fast;         // fast EMA indicator
   CiMA              m_ma_slow;         // slow EMA indicator
   int               m_fast_period;     // fast EMA period
   int               m_slow_period;     // slow EMA period
   ENUM_APPLIED_PRICE m_applied;        // applied price

public:
                     CSignalEMACross(void);
                    ~CSignalEMACross(void);
   //--- methods of setting adjustable parameters
   void              FastPeriod(int value)              { m_fast_period=value; }
   void              SlowPeriod(int value)              { m_slow_period=value; }
   void              Applied(ENUM_APPLIED_PRICE value)  { m_applied=value;     }
   //--- method of verification of settings
   virtual bool      ValidationSettings(void);
   //--- method of creating the indicators
   virtual bool      InitIndicators(CIndicators *indicators);
   //--- methods of checking if the market models are formed
   virtual int       LongCondition(void);
   virtual int       ShortCondition(void);

protected:
   double            FastMA(int ind) { return(m_ma_fast.Main(ind)); }
   double            SlowMA(int ind) { return(m_ma_slow.Main(ind)); }
  };
//+------------------------------------------------------------------+
//| Constructor                                                      |
//+------------------------------------------------------------------+
CSignalEMACross::CSignalEMACross(void) : m_fast_period(5),
                                         m_slow_period(13),
                                         m_applied(PRICE_CLOSE)
  {
  }
//+------------------------------------------------------------------+
//| Destructor                                                       |
//+------------------------------------------------------------------+
CSignalEMACross::~CSignalEMACross(void)
  {
  }
//+------------------------------------------------------------------+
//| Validation settings protected data.                              |
//+------------------------------------------------------------------+
bool CSignalEMACross::ValidationSettings(void)
  {
//--- validation settings of additional filters
   if(!CExpertSignal::ValidationSettings())
      return(false);
//--- initial data checks
   if(m_fast_period<=0 || m_slow_period<=0)
     {
      printf(__FUNCTION__+": EMA periods must be greater than 0");
      return(false);
     }
   if(m_fast_period>=m_slow_period)
     {
      printf(__FUNCTION__+": fast EMA period must be less than slow EMA period");
      return(false);
     }
//--- ok
   return(true);
  }
//+------------------------------------------------------------------+
//| Create indicators.                                               |
//+------------------------------------------------------------------+
bool CSignalEMACross::InitIndicators(CIndicators *indicators)
  {
//--- check pointer
   if(indicators==NULL)
      return(false);
//--- initialization of indicators and timeseries of additional filters
   if(!CExpertSignal::InitIndicators(indicators))
      return(false);
//--- create and initialize fast EMA indicator
   if(!indicators.Add(GetPointer(m_ma_fast)))
     {
      printf(__FUNCTION__+": error adding fast EMA object");
      return(false);
     }
   if(!m_ma_fast.Create(m_symbol.Name(),m_period,m_fast_period,0,MODE_EMA,m_applied))
     {
      printf(__FUNCTION__+": error initializing fast EMA object");
      return(false);
     }
//--- create and initialize slow EMA indicator
   if(!indicators.Add(GetPointer(m_ma_slow)))
     {
      printf(__FUNCTION__+": error adding slow EMA object");
      return(false);
     }
   if(!m_ma_slow.Create(m_symbol.Name(),m_period,m_slow_period,0,MODE_EMA,m_applied))
     {
      printf(__FUNCTION__+": error initializing slow EMA object");
      return(false);
     }
//--- ok
   return(true);
  }
//+------------------------------------------------------------------+
//| "Voting" that price will grow: fast EMA crosses above slow EMA.  |
//+------------------------------------------------------------------+
int CSignalEMACross::LongCondition(void)
  {
   int idx=StartIndex();
//--- fast EMA was at/below the slow EMA on the previous bar and is above it now
   if(FastMA(idx+1)<=SlowMA(idx+1) && FastMA(idx)>SlowMA(idx))
     {
      m_base_price=0.0;
      return(100);
     }
   return(0);
  }
//+------------------------------------------------------------------+
//| "Voting" that price will fall: fast EMA crosses below slow EMA.  |
//+------------------------------------------------------------------+
int CSignalEMACross::ShortCondition(void)
  {
   int idx=StartIndex();
//--- fast EMA was at/above the slow EMA on the previous bar and is below it now
   if(FastMA(idx+1)>=SlowMA(idx+1) && FastMA(idx)<SlowMA(idx))
     {
      m_base_price=0.0;
      return(100);
     }
   return(0);
  }
//+------------------------------------------------------------------+
//| Global expert object                                             |
//+------------------------------------------------------------------+
CExpert ExtExpert;
//+------------------------------------------------------------------+
//| Initialization function of the expert                            |
//+------------------------------------------------------------------+
int OnInit(void)
  {
//--- Initializing expert
   if(!ExtExpert.Init(Symbol(),Period(),Expert_EveryTick,Expert_MagicNumber))
     {
      //--- failed
      printf(__FUNCTION__+": error initializing expert");
      ExtExpert.Deinit();
      return(-1);
     }
//--- Creation of signal object
   CSignalEMACross *signal=new CSignalEMACross;
   if(signal==NULL)
     {
      //--- failed
      printf(__FUNCTION__+": error creating signal");
      ExtExpert.Deinit();
      return(-2);
     }
//--- Add signal to expert (will be deleted automatically))
   if(!ExtExpert.InitSignal(signal))
     {
      //--- failed
      printf(__FUNCTION__+": error initializing signal");
      ExtExpert.Deinit();
      return(-3);
     }
//--- Set signal parameters
   signal.FastPeriod(Inp_Signal_EMA_Fast_Period);
   signal.SlowPeriod(Inp_Signal_EMA_Slow_Period);
   signal.Applied(Inp_Signal_EMA_Applied);
//--- Check signal parameters
   if(!signal.ValidationSettings())
     {
      //--- failed
      printf(__FUNCTION__+": error signal parameters");
      ExtExpert.Deinit();
      return(-4);
     }
//--- Creation of trailing object
   CTrailingPSAR *trailing=new CTrailingPSAR;
   if(trailing==NULL)
     {
      //--- failed
      printf(__FUNCTION__+": error creating trailing");
      ExtExpert.Deinit();
      return(-5);
     }
//--- Add trailing to expert (will be deleted automatically))
   if(!ExtExpert.InitTrailing(trailing))
     {
      //--- failed
      printf(__FUNCTION__+": error initializing trailing");
      ExtExpert.Deinit();
      return(-6);
     }
//--- Set trailing parameters
   trailing.Step(Inp_Trailing_ParabolicSAR_Step);
   trailing.Maximum(Inp_Trailing_ParabolicSAR_Maximum);
//--- Check trailing parameters
   if(!trailing.ValidationSettings())
     {
      //--- failed
      printf(__FUNCTION__+": error trailing parameters");
      ExtExpert.Deinit();
      return(-7);
     }
//--- Creation of money object
   CMoneyFixedLot *money=new CMoneyFixedLot;
   if(money==NULL)
     {
      //--- failed
      printf(__FUNCTION__+": error creating money");
      ExtExpert.Deinit();
      return(-8);
     }
//--- Add money to expert (will be deleted automatically))
   if(!ExtExpert.InitMoney(money))
     {
      //--- failed
      printf(__FUNCTION__+": error initializing money");
      ExtExpert.Deinit();
      return(-9);
     }
//--- Set money parameters
   money.Lots(Inp_Money_FixedLot_Lots);
//--- Check money parameters
   if(!money.ValidationSettings())
     {
      //--- failed
      printf(__FUNCTION__+": error money parameters");
      ExtExpert.Deinit();
      return(-10);
     }
//--- Tuning of all necessary indicators
   if(!ExtExpert.InitIndicators())
     {
      //--- failed
      printf(__FUNCTION__+": error initializing indicators");
      ExtExpert.Deinit();
      return(-11);
     }
//--- succeed
   return(INIT_SUCCEEDED);
  }
//+------------------------------------------------------------------+
//| Deinitialization function of the expert                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   ExtExpert.Deinit();
  }
//+------------------------------------------------------------------+
//| Function-event handler "tick"                                    |
//+------------------------------------------------------------------+
void OnTick(void)
  {
   ExtExpert.OnTick();
  }
//+------------------------------------------------------------------+
//| Function-event handler "trade"                                   |
//+------------------------------------------------------------------+
void OnTrade(void)
  {
   ExtExpert.OnTrade();
  }
//+------------------------------------------------------------------+
//| Function-event handler "timer"                                   |
//+------------------------------------------------------------------+
void OnTimer(void)
  {
   ExtExpert.OnTimer();
  }
//+------------------------------------------------------------------+
