//+------------------------------------------------------------------+
//| GRE_Shot.mq5 — снимок графика по команде Telegram-бота (фаза 3)  |
//|                                                                    |
//| Python-бот (mt5bot\) пишет в общий каталог терминалов (FILE_COMMON)|
//| файл GRE_shot_cmd.txt вида "<nonce> [символ]". Каждый экземпляр     |
//| GRE_Shot (по одному на график) опрашивает его раз в секунду; новый |
//| nonce -> ChartScreenShot() СВОЕГО графика в                        |
//| MQL5\Files\GREshot_<sym>_<nonce>.png (ChartScreenShot в Common     |
//| писать не умеет — песочница), бот забирает PNG и шлёт send_photo.  |
//|                                                                    |
//| Командный файл индикатор НЕ удаляет: nonce живёт в «почтовом       |
//| ящике» до перезаписи следующим запросом — все графики успевают его |
//| прочитать (гонки с удалением нет), повторного срабатывания тоже    |
//| нет (latch: свой последний отработанный nonce). Бот подчищает      |
//| и файл, и чужие PNG сам.                                           |
//|                                                                    |
//| Discovery (v1.12): команда "<nonce> ?" — «кто жив?» — вместо       |
//| снимка каждый экземпляр пишет крошечный GREalive_<sym>_<nonce>.txt |
//| в свою песочницу: бот строит меню графиков без раунда снимков.     |
//|                                                                    |
//| Видимость (v1.13): сам индикатор ничего не рисует (plots 0),       |
//| вместо него — бейдж с тикающими часами в ПРАВОМ верхнем углу,      |
//| ПОД именем советника — его рисует сам терминал (строка с иконкой   |
//| в правом углу). Координаты: y=52 — строкой ниже блока имени, x=80  |
//| от правого края — левее шкалы цен; якорь правый, текст растёт      |
//| влево и не задевает дашборд EA (верхний ЛЕВЫЙ стек Dashboard.mqh; |
//| v1.10 в этом углу стояла вплотную к верху и наползала на имя EA,   |
//| v1.12 — в левом блоке дашборда; вернули под имя, в правый угол).  |
//| Секунды идут = опрос жив; после снимка — «last: символ время».     |
//| Comment() НЕ используется: он один на график и затёр бы дашборд.   |
//+------------------------------------------------------------------+
#property copyright "GRE project"
#property version   "1.13"
#property indicator_chart_window
#property indicator_plots 0

input int InpPollSec = 1;   // период опроса командного файла, сек

string g_lastNonce = "";
string g_badge = "GRE_SHOT_BADGE";   // уникальное имя — не мешает объектам EA
string g_lastShot = "";              // «<символ> <время>» последнего снимка

void UpdateBadge()
{
   string line = "GRE_Shot · " + TimeToString(TimeLocal(), TIME_SECONDS);
   if(g_lastShot != "")
      line += " · last: " + g_lastShot;
   ObjectSetString(0, g_badge, OBJPROP_TEXT, line);
   ChartRedraw(0);
}

//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(MathMax(1, InpPollSec));
   ObjectCreate(0, g_badge, OBJ_LABEL, 0, 0, 0);
   // Под именем советника в ПРАВОМ верхнем углу: имя EA («GridReversalEA»
   // с иконкой) терминал рисует сам (~y=5..25); бейдж — строкой ниже (y=52),
   // правый якорь, x=80 от края — левее шкалы цен; текст растёт влево и
   // не задевает ни имя EA, ни левый стек дашборда.
   ObjectSetInteger(0, g_badge, OBJPROP_CORNER, CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, g_badge, OBJPROP_ANCHOR, ANCHOR_RIGHT_UPPER);
   ObjectSetInteger(0, g_badge, OBJPROP_XDISTANCE, 80);   // левее шкалы цен справа
   ObjectSetInteger(0, g_badge, OBJPROP_YDISTANCE, 52);   // строкой ниже имени EA
   ObjectSetInteger(0, g_badge, OBJPROP_COLOR, clrSilver);
   ObjectSetInteger(0, g_badge, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, g_badge, OBJPROP_FONT, "Consolas");  // в стиле дашборда EA
   ObjectSetInteger(0, g_badge, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, g_badge, OBJPROP_HIDDEN, true);
   ObjectSetString(0, g_badge, OBJPROP_TEXT, "GRE_Shot · старт");
   ChartRedraw(0);
   Print("GRE_Shot started: polling GRE_shot_cmd.txt every ",
         MathMax(1, InpPollSec), "s; shots -> MQL5\\Files\\GREshot_<sym>_<nonce>.png");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectDelete(0, g_badge);   // не оставлять призрак на графике
   ChartRedraw(0);
}

//| Обязательная заглушка индикатора: рисовать ничего не нужно.       |
int OnCalculate(const int rates_total, const int prev_calculated,
                const datetime &time[], const double &open[],
                const double &high[], const double &low[],
                const double &close[], const long &tick_volume[],
                const long &volume[], const int &spread[])
{
   return(rates_total);
}

void OnTimer()
{
   UpdateBadge();   // тикающие секунды = опрос жив (до всех ранних return)
   int h = FileOpen("GRE_shot_cmd.txt",
                    FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON
                    | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return;                            // команды нет — тихо ждём
   string cmd = FileReadString(h);
   FileClose(h);

   string parts[];
   if(StringSplit(cmd, ' ', parts) < 1 || parts[0] == "")
      return;
   string nonce = parts[0];
   // Защита от «грязи» в строке: текстовая запись из Python в Windows умеет
   // удваивать \r (CRLF -> CRCRLF), и хвостовой \r из строки попадал в ИМЯ
   // PNG-файла — снимок делался, но бот его потом не находил. Чистим оба поля.
   StringReplace(nonce, "\r", "");
   StringReplace(nonce, "\n", "");
   if(nonce == "")
      return;
   if(nonce == g_lastNonce)
      return;                            // этот запрос уже отработан этим графиком
   g_lastNonce = nonce;

   string want = (ArraySize(parts) > 1) ? parts[1] : "";
   StringReplace(want, "\r", "");
   StringReplace(want, "\n", "");
   if(want == "?")
   {
      // Discovery round: report THIS chart instead of taking a screenshot —
      // lets the bot list every GRE_Shot chart without a photo round.
      string alive = StringFormat("GREalive_%s_%s.txt", _Symbol, nonce);
      int ah = FileOpen(alive, FILE_WRITE | FILE_TXT | FILE_ANSI);
      if(ah != INVALID_HANDLE)
      {
         FileWriteString(ah, _Symbol + " " + EnumToString((ENUM_TIMEFRAMES)Period()));
         FileClose(ah);
         Print("GRE_Shot: alive -> ", alive);
      }
      g_lastShot = _Symbol + " scan";
      UpdateBadge();
      return;
   }
   if(want != "" && StringCompare(want, _Symbol, false) != 0)
   {
      Print("GRE_Shot: request for ", want, ", this chart is ", _Symbol, " — skipped");
      return;                            // запрошен другой символ
   }

   string fname = StringFormat("GREshot_%s_%s.png", _Symbol, nonce);
   if(!ChartScreenShot(0, fname, 1280, 720))
      Print("GRE_Shot: ChartScreenShot failed, err=", GetLastError());
   else
   {
      g_lastShot = _Symbol + " " + TimeToString(TimeLocal(), TIME_SECONDS);
      Print("GRE_Shot: shot saved -> ", fname);
   }
   UpdateBadge();
}
//+------------------------------------------------------------------+
