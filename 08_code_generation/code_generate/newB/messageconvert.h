#ifndef MESSAGECONVERT_H
#define MESSAGECONVERT_H
#include <QObject>
#include<memory>
#include<QUdpSocket>
#include <QMutex>
#include <QTimer>
class messageConvert : public QObject
    {
        Q_OBJECT
    public:
        explicit messageConvert(QObject* parent = nullptr);
    public:
        enum NetType
        {
            emTCP,
            emUDP,
            emDDS
        };
        class NetInfo
        {
        public:
            QString    name;
            QString    ip;
            int        port;
            quint16      feedBackPort;
            int        netType;
            bool       bRecvTag = true;
        };
        class msgDataInfo
            {
            public:
                QByteArray  data;
                QVector<qulonglong>   time;
                QString      name;
                QString      ip;
                quint16      port;
                QStringList  state={};
                int  num = 0;
            };
    signals:
        void   showMessage(QString msg);
    public Q_SLOTS:
        void readPendingDatagrams(QString name, QHostAddress ip, quint16 port, QByteArray data);
        void onCheckDataTimer();
    private:
        int      _maxThread = 5;
        int                        _threadExit = 0;
        std::shared_ptr<QUdpSocket> udpSend;
        QVector<std::shared_ptr<NetInfo> >  udpSendList;
        QVector<std::shared_ptr<QUdpSocket>>udpRecvList;
           QVector<std::shared_ptr<msgDataInfo>>  dataInfo;
           QMutex                                 dataMutex;
           QTimer                                 checkDataTimer;
    private:
        void   pushData(std::shared_ptr<msgDataInfo> data);
        void   getData(QString name,int time, int num, QByteArray & data,QString & ip,int & port,int & outTime);
        void   checkData(QString name,int time);
        void msgConvertThread();
        void msgSendThread();
        void onSendMessage(QByteArray msg);
		void S0_1dataPro();
		void S106dataPro();
    public:
        int start(QVector<std::shared_ptr<NetInfo>> netlist, int maxThread = 5);
        int stop();
    };
#endif // MESSAGECONVERT_H
