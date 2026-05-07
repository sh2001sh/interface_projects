#include "messageconvert.h"
#include <qdatetime.h>
#include "codec.h"
#include <QtConcurrent>
#include "to_code_Choreography.h"
 messageConvert::messageConvert(QObject * parent)
        : QObject{ parent }
    {}
int messageConvert::start(QVector<std::shared_ptr<NetInfo> > netlist, int maxThread)
    {
        udpSend.reset(new QUdpSocket());
        for (auto serv : netlist)
        {
            if (serv->bRecvTag == false)
            {
                udpSendList.push_back(serv);
            }
            else
            {
                std::shared_ptr<QUdpSocket> soc(new QUdpSocket);
                connect(soc.get(), &QUdpSocket::readyRead, [serv,soc, this]() {
                    while (soc->hasPendingDatagrams()) {
                        QHostAddress sender;
                        quint16 senderPort;
                        qint64 size = soc->pendingDatagramSize();
                        QByteArray buffer(size, 0);
                        soc->readDatagram(buffer.data(), size, &sender, &senderPort);
                        qDebug() << __FUNCTION__ << "  time:" << QDateTime::currentMSecsSinceEpoch() << "  Received data from" << sender << ":" << senderPort << " with length :" << buffer.size();
                        readPendingDatagrams(serv->name,sender, serv->feedBackPort, buffer);
                    }
                    });
                if (!soc->bind(QHostAddress::Any, serv->port))
                {
                    qDebug() << __FUNCTION__ << "Failed to bind to port" << serv->port << soc->errorString();
                    return -1;
                }
                udpRecvList.push_back(soc);
            }
        }
        QtConcurrent::run([this]() {
        this->msgConvertThread();
        qDebug() << __FUNCTION__ << "   tiem: " << QDateTime::currentMSecsSinceEpoch() << "  Thread exit. ";
 });
connect(&checkDataTimer,&QTimer::timeout,this,&messageConvert::onCheckDataTimer);
checkDataTimer.start(5000);//检查目的消息数据有效性定时器
        return 0;
    }
void messageConvert::onCheckDataTimer()
{
    int  time = 0;
	time = code_test::getDstMsg_41(u8"W协议.W30_4");
	checkData(u8"W30_4", time); 
	time = code_test::getDstMsg_41(u8"W协议.W20_4");
	checkData(u8"W20_4", time); 
}
    int messageConvert::stop()
    {
         _threadExit = 1;
         checkDataTimer.stop();
        for (auto var : udpRecvList)
        {
            if (var->isOpen())
                var->close();
        }
        if (udpSend->isOpen())
            udpSend->close();
        udpRecvList.clear();
        return 0;
    }
    void messageConvert::onSendMessage(QByteArray msg)
    {
        for (auto var : udpSendList)
        {
            qint64 len = udpSend->writeDatagram(msg, QHostAddress(var->ip), var->port);
            qDebug() << "send len:" << len << "ip: " << var->ip << "  port: " << var->port;
        }
    }
    void messageConvert::readPendingDatagrams(QString name,QHostAddress ip, quint16 port, QByteArray data)
    {
        qDebug() << __FUNCTION__ << "   tiem: " << QDateTime::currentMSecsSinceEpoch() << "  " << ip << " : " << port << "  data: " << data.toHex();
            std::shared_ptr<msgDataInfo> d(new msgDataInfo);
            d->time .append(QDateTime::currentMSecsSinceEpoch());
            d->name = name;
            d->num = 1;
            d->data = data;
            d->ip = ip.toString();
            d->port = port;
            pushData(d);
    }
void   messageConvert::pushData(std::shared_ptr<msgDataInfo> data)
    {
        QMutexLocker lock(&dataMutex);
        for (int i = 0; i < dataInfo.size(); i++)
        {
            if (data->name == dataInfo[i]->name)
            {
                if (data->data != dataInfo[i]->data)
                {
                    dataInfo[i] = data;
                    dataInfo[i]->time=data->time;
                    dataInfo[i]->state.clear();
                }
                else if (data->data == dataInfo[i]->data)
                {
                    dataInfo[i]->num++;
                    dataInfo[i]->state.clear();
                }
                return;
            }
        }
        dataInfo.push_back(data);
    }
     void   messageConvert::checkData(QString name, int time)
     {
         QMutexLocker lock(&dataMutex);
         for (int i = 0; i < dataInfo.size(); i++)
         {
             int ll = QDateTime::currentMSecsSinceEpoch() - dataInfo[i]->time.last();
             if (ll > time && name == dataInfo[i]->name)
             {
                 qDebug() << __FUNCTION__ << "   tiem: " << QDateTime::currentMSecsSinceEpoch() << " remove data. name:" << name;
                 dataInfo.remove(i);
                 return;
             }
         }
     }
    void   messageConvert::getData(QString name,int time, int num, QByteArray & data,QString & ip,int & port,int & outTime)
    {
        QMutexLocker lock(&dataMutex);
        for (auto item : dataInfo)
        {
            if (name == item->name && (num == item->num) && item->state.indexOf(name) == -1)
            {
                  for (int i = item->time.size() - 1; i >= 1; i--)
                  {
                      if (item->time[i] - item->time[i - 1] <= time)
                      {
                          return;
                      }
                  }
                 ip = item->ip;
                 port = item->port;
                data = item->data;
                item->state.append(name);
                outTime = item->time.first();
                return;
            }
            else
            {
                qDebug() << __FUNCTION__ << "   tiem: " << QDateTime::currentMSecsSinceEpoch() << "  dest:" << name << " : " << num << "  src: " << item->name << " " << item->num;
            }
        }
    }
void messageConvert::S0_1dataPro()
{
	QStringList msgNameList;
	QVector<int> msgTimeList;
	 QByteArray  s0_1data;
	S0_1  s0_1={0};
	int s0_1Flag = 0;
	QString s0_1Ip;
	int s0_1Port = 0;
	int s0_1Time = 0;
	 int counts0_1[  6]={ 1, 1, 1, 1, 1, 1, };
	 int cycles0_1[ 6]={ 0, 0, 0, 0, 0, 0, };
	int nums0_1 = 6;
	while(nums0_1-- > 0)
	{ 
		getData("S01",cycles0_1[nums0_1],counts0_1[nums0_1], s0_1data,s0_1Ip,s0_1Port,s0_1Time); 
		if( s0_1data.isEmpty() == false )
		{
		
			QString ret=decodeMsg((uchar*) s0_1data.data(),s0_1data.size(),s0_1);
			if(ret.isEmpty() == false) {
				 QByteArray sdata;
				 int iret = checkObjMaps(ret, sdata, s0_1); 
				 if(iret ==0){ s0_1Flag = 1;}
				 if(iret !=-1&&s0_1Port > 0){ 
					QUdpSocket soc;
					soc.writeDatagram(sdata,QHostAddress(s0_1Ip), s0_1Port); 
				}
			}
			break;  
		 }
	}
	if(1 != s0_1Flag)
	{
		return; 
	}
	msgNameList.append(u8"S协议.S0_1");
	msgTimeList.append(s0_1Time); 
	if (msgNameList.size() >= 2) {
	    int state = 0;
	    for (int i = 0; i < msgNameList.size() - 1; i++) {
	        for (int j = i + 1; j < msgNameList.size(); j++) {
		         int s = code_test::getSrcTime_41(msgNameList[i], msgNameList[j]);
		         if (-1 == s || (s + (msgTimeList[i] - msgTimeList[j])) > 0)
		         {
		            state += 1;
		         }
	        }
	    }
	    if (msgNameList.size() != state + 1)
	    {
		        return;//说明消息时间不对，不能进行转换
	    }
	}
	W304  w304  =convert_s0_1_to_w304(s0_1); 
	QByteArray sendData;
	encodeMsg(sendData, w304);
	code_test check;
	int sflag = check.getStatus_41(u8"W协议.W30_4");
	if (0 == sflag)
		onSendMessage(sendData);
	else
	{
		std::shared_ptr<msgDataInfo> d(new msgDataInfo);
		d->time.append(QDateTime::currentMSecsSinceEpoch());
		d->name = "W30_4";
		d->num = 3;
		d->data = sendData;
		d->ip = "127.0.0.1";
		d->port = 0;
		pushData(d);
	}
}
void messageConvert::S106dataPro()
{
	QStringList msgNameList;
	QVector<int> msgTimeList;
	 QByteArray  s106data;
	S106  s106={0};
	int s106Flag = 0;
	QString s106Ip;
	int s106Port = 0;
	int s106Time = 0;
	 int counts106[  2]={ 3, 3, };
	 int cycles106[ 2]={ 100, 100, };
	int nums106 = 2;
	while(nums106-- > 0)
	{ 
		getData("S106",cycles106[nums106],counts106[nums106], s106data,s106Ip,s106Port,s106Time); 
		if( s106data.isEmpty() == false )
		{
		
			QString ret=decodeMsg((uchar*) s106data.data(),s106data.size(),s106);
			if(ret.isEmpty() == false) {
				 QByteArray sdata;
				 int iret = checkObjMaps(ret, sdata, s106); 
				 if(iret ==0){ s106Flag = 1;}
				 if(iret !=-1&&s106Port > 0){ 
					QUdpSocket soc;
					soc.writeDatagram(sdata,QHostAddress(s106Ip), s106Port); 
				}
			}
			break;  
		 }
	}
	if(1 != s106Flag)
	{
		return; 
	}
	msgNameList.append(u8"S协议.S106");
	msgTimeList.append(s106Time); 
	if (msgNameList.size() >= 2) {
	    int state = 0;
	    for (int i = 0; i < msgNameList.size() - 1; i++) {
	        for (int j = i + 1; j < msgNameList.size(); j++) {
		         int s = code_test::getSrcTime_41(msgNameList[i], msgNameList[j]);
		         if (-1 == s || (s + (msgTimeList[i] - msgTimeList[j])) > 0)
		         {
		            state += 1;
		         }
	        }
	    }
	    if (msgNameList.size() != state + 1)
	    {
		        return;//说明消息时间不对，不能进行转换
	    }
	}
	W204  w204  =convert_s106_to_w204(s106); 
	QByteArray sendData;
	encodeMsg(sendData, w204);
	code_test check;
	int sflag = check.getStatus_41(u8"W协议.W20_4");
	if (0 == sflag)
		onSendMessage(sendData);
	else
	{
		std::shared_ptr<msgDataInfo> d(new msgDataInfo);
		d->time.append(QDateTime::currentMSecsSinceEpoch());
		d->name = "W20_4";
		d->num = 3;
		d->data = sendData;
		d->ip = "127.0.0.1";
		d->port = 0;
		pushData(d);
	}
}
    void  messageConvert::msgConvertThread()
    {
        while (0 == _threadExit)
        {
			S0_1dataPro();
			S106dataPro();
            _sleep(2);
        }
    }