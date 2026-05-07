#include "messageconvert.h"
#include <qdatetime.h>
#include "codec.h"
#include <QtConcurrent>
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
        return 0;
    }
    int messageConvert::stop()
    {
         _threadExit = 1;
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
            qDebug() << "send len:" << len << "ip: " << var->ip << "  port: " << var->port<< "msglen:" << msg.size() << "  data : " << msg.toHex();
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
                    dataInfo[i]->state=0;
                }
                else if (data->data == dataInfo[i]->data)
                {
                    dataInfo[i]->num++;
                    dataInfo[i]->state=0;
                }
                return;
            }
        }
        dataInfo.push_back(data);
    }
    void   messageConvert::getData(QString name,int time, int num, QByteArray & data,QString & ip,int & port)
    {
        QMutexLocker lock(&dataMutex);
        for (auto item : dataInfo)
        {
            if (name == item->name && (num <= item->num) && item->state == 0)
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
                item->state=1;
                return;
            }
            else
            {
               // qDebug() << __FUNCTION__ << "   tiem: " << QDateTime::currentMSecsSinceEpoch() << "  dest:" << name << " : " << num << "  src: " << item->name << " " << item->num;
            }
        }
    }
void messageConvert::W30_4dataPro()
{
	QStringList msgNameList;
	QVector<int> msgTimeList;
	 QByteArray  w304data;
	W304  w304={0};
	int w304Flag = 0;
	QString w304Ip;
	int w304Port;
	 int countw304[  3]={ 1, 3, 3, };
	 int cyclew304[ 3]={ 0, 2000, 2000, };
	int numw304 = 3;
	while(numw304-- > 0)
	{ 
		getData("W304",cyclew304[numw304],countw304[numw304], w304data,w304Ip,w304Port); 
		if( w304data.isEmpty() == false )
		{
		
			QString ret=decodeMsg((uchar*) w304data.data(),w304data.size(),w304);
			if(ret.isEmpty() == false) {
				 QByteArray sdata;
				 int iret = checkObjMaps(ret, sdata, w304); 
				 if(iret ==0){ w304Flag = 1;}
				 if(iret !=-1){ 
					QUdpSocket soc;
					soc.writeDatagram(sdata,QHostAddress(w304Ip), w304Port); 
				}
			}
			break;  
		 }
	}
	if(1 != w304Flag)
	{ 
		return; 
	}
	{
		ICD304  iCD304  =convert_w304_to_iCD304(w304); 
		QByteArray sendData;
		encodeMsg(sendData, iCD304);
		onSendMessage(sendData);
	}
}
    void  messageConvert::msgConvertThread()
    {
        while (0 == _threadExit)
        {
			W30_4dataPro();
            _sleep(2);
        }
    }